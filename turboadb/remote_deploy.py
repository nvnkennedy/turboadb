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

import os
import sys
import subprocess
import time

from .config import validate_port

# Per-host WinRM budget, and the budget for a whole run. 20 unreachable hosts at
# 120 s each used to mean ~40 minutes behind a modal dialog.
HOST_TIMEOUT = 120.0
TOTAL_TIMEOUT = 600.0
MAX_WORKERS = 4

# Reading the password from the environment (or stdin) keeps it out of argv,
# where the process table and the shell history expose it to every local user.
PASSWORD_ENV = "TURBOADB_DEPLOY_PASSWORD"


def resolve_password(
    password: str | None = None,
    *,
    from_stdin: bool = False,
    env_var: str = PASSWORD_ENV,
    prompt: bool = True,
    user: str = "",
) -> str:
    """The WinRM password, preferring the sources that never reach ``argv``.

    Order: an explicit *password*, one line read from **stdin** (*from_stdin*),
    ``$TURBOADB_DEPLOY_PASSWORD``, then an interactive prompt. A password given
    on the command line is visible in the process table and the shell history —
    and therefore to any local user, on a machine whose admin credential also
    unlocks every target — so callers should offer the other three first.

    Raises ValueError when nothing is available and prompting is disabled."""
    if password:
        return password
    if from_stdin:
        line = sys.stdin.readline()
        if not line:
            raise ValueError("no password was provided on stdin")
        return line.rstrip("\r\n")
    from_env = os.environ.get(env_var)
    if from_env:
        return from_env
    if not prompt:
        raise ValueError(
            f"no password given — pass it on stdin or set {env_var} "
            "(a password in argv is visible to every local user)"
        )
    import getpass

    return getpass.getpass(f"Password for {user or 'the remote admin'}: ")


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
    host_timeout=None,
):
    import winrm

    scheme = "https" if use_ssl else "http"
    endpoint = f"{scheme}://{host}:{winrm_port}/wsman"
    # Bound the timeouts so an unreachable / non-responding host fails in a known
    # time instead of hanging the (now modal) deploy popup. 120 s is plenty for a
    # dependency-free `pip install -U turboadb` + starting the server; read_timeout
    # must exceed operation_timeout (a pywinrm requirement).
    op = operation_timeout
    if op is None:
        op = int(host_timeout if host_timeout else HOST_TIMEOUT)
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


def _run_ps(host, login, password, script, *, winrm_port, use_ssl=False, host_timeout=None):
    session = _session(
        host,
        login,
        password,
        winrm_port=winrm_port,
        use_ssl=use_ssl,
        host_timeout=host_timeout,
    )
    r = session.run_ps(script)
    out = (r.std_out or b"").decode("utf-8", "replace").strip()
    err = (r.std_err or b"").decode("utf-8", "replace").strip()
    return r.status_code, out, err


def _deploy_one(
    host, username, password, *, update, port, test_only, winrm_port, use_ssl, host_timeout
) -> tuple:
    """Deploy to (or test) ONE host. Returns ``(ok, [status lines])``.

    Never raises: one unreachable host must not abort the rest of the batch, and
    the lines are handed back to the caller so only the calling thread touches
    the caller's *on_status* callback."""
    lines = [f"[INFO] {host}: connecting over WinRM (NTLM) as {username}…"]
    try:
        if test_only:
            code, out, err = _run_ps(
                host,
                username,
                password,
                "'OK:'+$env:COMPUTERNAME",
                winrm_port=winrm_port,
                use_ssl=use_ssl,
                host_timeout=host_timeout,
            )
            if code == 0 and "OK:" in out:
                name = out.split("OK:", 1)[1].strip().splitlines()[0]
                lines.append(f"[OK] {host}: WinRM reachable, credentials accepted (remote = {name})")
                return True, lines
            lines.append(f"[ERROR] {host}: connected but check failed: {(err or out)[:200]}")
            return False, lines

        script = _DEPLOY_PS.format(upd="1" if update else "0", port=port)
        code, out, err = _run_ps(
            host,
            username,
            password,
            script,
            winrm_port=winrm_port,
            use_ssl=use_ssl,
            host_timeout=host_timeout,
        )
        for line in out.splitlines():
            if line.startswith("WARNING:"):
                lines.append(f"[WARNING] {host}: {line[len('WARNING:'):].strip()[:240]}")
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
            lines.append(f"[OK] {host}: serve started — {detail[:220]}")
            return True, lines
        emsg = next(
            (
                line[len("ERROR:") :].strip()
                for line in out.splitlines()
                if line.startswith("ERROR:")
            ),
            (err or out),
        )
        lines.append(f"[ERROR] {host}: {emsg[:240]}")
        return False, lines
    except Exception as exc:
        lines.append(f"[ERROR] {host}: WinRM failed: {exc}")
        lines.append(
            f"[INFO] {host}: make sure WinRM is on there (run on it as admin: "
            f"Enable-PSRemoting -Force), your account is a local admin, and "
            f"the user is in DOMAIN\\user form."
        )
        return False, lines


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
    host_timeout=HOST_TIMEOUT,
    total_timeout=TOTAL_TIMEOUT,
    max_workers=MAX_WORKERS,
) -> int:
    """Deploy (or, with *test_only*, just verify WinRM/credentials on) *hosts* —
    one or many. *username* should be ``DOMAIN\\user``. Streams leveled status
    lines to *on_status*. Returns 0 if every host succeeded, else 1.

    WinRM sessions are independent, so up to *max_workers* hosts are contacted
    at once and the whole run is bounded by *total_timeout* seconds. Hosts that
    the budget ran out for are reported as SKIPPED (and count as failures) —
    previously 20 unreachable hosts meant ~40 minutes of a frozen modal dialog.
    Pass ``max_workers=1`` for the old strictly serial behaviour, or
    ``total_timeout=None`` for no overall budget. Results are always reported in
    the order the hosts were given."""
    from concurrent.futures import ThreadPoolExecutor

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

    workers = max(1, min(int(max_workers or 1), len(hosts)))
    if len(hosts) > 1:
        budget = f"{total_timeout:g}s budget" if total_timeout else "no overall budget"
        say(f"[INFO] {len(hosts)} host(s), {workers} at a time ({budget}).")
    deadline = (time.monotonic() + total_timeout) if total_timeout else None

    rc = 0
    skipped = []
    pool = ThreadPoolExecutor(max_workers=workers)
    try:
        futures = [
            pool.submit(
                _deploy_one,
                h,
                username,
                password,
                update=update,
                port=port,
                test_only=test_only,
                winrm_port=winrm_port,
                use_ssl=use_ssl,
                host_timeout=host_timeout,
            )
            for h in hosts
        ]
        for index, (host, future) in enumerate(zip(hosts, futures)):
            left = None if deadline is None else max(0.0, deadline - time.monotonic())
            try:
                if left == 0.0:
                    raise TimeoutError
                ok, lines = future.result(timeout=left)
            except Exception:
                # The budget is gone: drop every host that has not finished
                # rather than waiting out one 120 s WinRM timeout after another.
                skipped = hosts[index:]
                for pending in futures[index:]:
                    pending.cancel()
                break
            for line in lines:
                say(line)
            if not ok:
                rc = 1
    finally:
        # never block the caller (a modal dialog) on a straggler's own timeout
        pool.shutdown(wait=False)
    if skipped:
        rc = 1
        budget = f"the {total_timeout:g}s budget ran out" if total_timeout else "the run was cut short"
        say(f"[WARNING] Skipped {len(skipped)} host(s) — {budget}: {', '.join(skipped)}")
    return rc
