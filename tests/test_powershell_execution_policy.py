"""The embedded PowerShell follows this PC's execution policy, like a
PowerShell window: TurboADB's startup script goes in with -Command, which no
policy blocks, so nothing needs -ExecutionPolicy Bypass."""

import os
import subprocess

import pytest

from turboadb.gui import local_terminal as lt


def _with(env, name, value):
    """*env* with *name* set, whatever the spelling of its key (Windows'
    ``os.environ`` spells them all in capitals)."""
    out = {k: v for k, v in env.items() if k.upper() != name.upper()}
    out[name] = value
    return out


def _get(env, name):
    return next((v for k, v in env.items() if k.upper() == name.upper()), None)


def _shell_env(base=None, **extra):
    """The environment TurboADB gives its PowerShell.  A test started from
    pwsh (GitHub's runners) inherits pwsh's PSModulePath, as TurboADB does
    when a pwsh terminal starts it."""
    env = lt.build_shell_env(base if base is not None else os.environ, windows=True)
    for name, value in extra.items():
        env = _with(env, name, value)
    return env


def test_powershell_keeps_the_execution_policy_of_the_pc():
    argv = lt.shell_argv("powershell", r"C:\Windows", columns=150)
    assert "-ExecutionPolicy" not in argv and "Bypass" not in argv
    assert argv[-2] == "-Command" and argv[-1] == lt._ps_init(150)


def test_powershell_7s_module_folders_are_known():
    pwsh_homes = {r"c:\program files\powershell\7-preview\pwsh.dll"}

    def isfile(path):
        return path.lower() in pwsh_homes

    for entry in (r"C:\Users\u\Documents\PowerShell\Modules",
                  r"C:\Users\u\OneDrive\Documents\PowerShell\Modules\\",
                  r"C:\Program Files\PowerShell\Modules",
                  r'"C:\Program Files\PowerShell\7-preview\Modules"'):
        assert lt.is_pwsh_module_path(entry, isfile), entry
    for entry in (r"C:\Users\u\Documents\WindowsPowerShell\Modules",
                  r"C:\Program Files\WindowsPowerShell\Modules",
                  r"C:\WINDOWS\system32\WindowsPowerShell\v1.0\Modules",
                  r"D:\tools\modules", "Modules", "C:\\"):
        assert not lt.is_pwsh_module_path(entry, isfile), entry


def test_the_shells_get_windows_powershells_module_folders_only():
    inherited = {"PATH": r"C:\Windows\System32", "PSModulePath": ";".join([
        r"C:\Users\u\Documents\PowerShell\Modules",
        r"C:\Program Files\PowerShell\Modules",
        r"C:\Program Files\WindowsPowerShell\Modules",
        r"C:\WINDOWS\system32\WindowsPowerShell\v1.0\Modules",
        r"D:\mine\Modules",
    ])}
    env = lt.build_shell_env(inherited, windows=True)
    assert env["PSModulePath"] == ";".join([
        r"C:\Program Files\WindowsPowerShell\Modules",
        r"C:\WINDOWS\system32\WindowsPowerShell\v1.0\Modules",
        r"D:\mine\Modules",  # the user's own stays
    ])
    only_pwsh = dict(inherited, PSModulePath=r"C:\Program Files\PowerShell\Modules")
    assert "PSModulePath" not in lt.build_shell_env(only_pwsh, windows=True)  # it builds its own
    assert lt.build_shell_env(inherited, windows=False)["PSModulePath"] == inherited["PSModulePath"]


@pytest.mark.skipif(os.name != "nt", reason="starts a real powershell.exe")
def test_pwshs_psreadline_inherited_from_a_pwsh_terminal_is_left_out(tmp_path):
    """Started from pwsh (a Windows Terminal tab; GitHub's runners), TurboADB
    inherits pwsh's module folders.  Windows PowerShell loaded pwsh's own
    PSReadLine from them, which fails there ("Cannot load PSReadline
    module"), and the first command was lost."""
    home = tmp_path / "PowerShell" / "7"  # a pwsh installation
    module = home / "Modules" / "PSReadLine"
    module.mkdir(parents=True)
    (home / "pwsh.dll").write_bytes(b"")
    (module / "Microsoft.PowerShell.PSReadLine2.dll").write_bytes(b"MZ" + bytes(4094))  # not for .NET 4
    (module / "PSReadLine.psd1").write_text(
        "@{ ModuleVersion = '2.3.6'; RootModule = 'Microsoft.PowerShell.PSReadLine2.dll'; "
        "GUID = '5714753b-2afd-4492-a5fd-01d9e2cff8b5' }")
    inherited = _with(os.environ, "PSModulePath",
                      f"{home / 'Modules'};{_get(os.environ, 'PSModulePath') or ''}")
    env = _shell_env(inherited)
    assert str(home).lower() not in (_get(env, "PSModulePath") or "").lower()
    proc = subprocess.Popen(
        lt.shell_argv("powershell", os.environ.get("SystemRoot"), columns=150),
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        env=env, creationflags=lt.NO_WINDOW,
    )
    out, _ = proc.communicate(b"'first=' + (1 + 1)\nexit\n", timeout=60)
    text = out.decode("utf-8", "replace")
    assert "Cannot load PSReadline" not in text and "first=2" in text, text


@pytest.mark.skipif(os.name != "nt", reason="starts a real powershell.exe")
@pytest.mark.parametrize("policy", ["Restricted", "AllSigned"])
def test_the_startup_script_runs_where_scripts_are_blocked(policy):
    env = _shell_env(PSExecutionPolicyPreference=policy)  # this process only
    proc = subprocess.Popen(
        lt.shell_argv("powershell", os.environ.get("SystemRoot"), columns=150),
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        env=env, creationflags=lt.NO_WINDOW,
    )
    script = (
        "'policy=' + (Get-ExecutionPolicy)\n"
        "'marked=' + ((Get-Command prompt).Definition -match '7717')\n"
        "'read-host=' + (Get-Command Read-Host).CommandType\n"
        "'width=' + $Host.UI.RawUI.BufferSize.Width\n"
        "exit\n"
    )
    out, _ = proc.communicate(script.encode("utf-8"), timeout=60)
    text = out.decode("utf-8", "replace")
    assert f"policy={policy}" in text
    assert "marked=True" in text  # the prompt mark the terminal follows
    assert "read-host=Function" in text  # the Read-Host that shows its prompt
    assert "width=150" in text
