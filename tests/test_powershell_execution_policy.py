"""The embedded PowerShell follows this PC's execution policy, like a
PowerShell window: TurboADB's startup script goes in with -Command, which no
policy blocks, so nothing needs -ExecutionPolicy Bypass."""

import os
import subprocess

import pytest

from turboadb.gui import local_terminal as lt


def test_powershell_keeps_the_execution_policy_of_the_pc():
    argv = lt.shell_argv("powershell", r"C:\Windows", columns=150)
    assert "-ExecutionPolicy" not in argv and "Bypass" not in argv
    assert argv[-2] == "-Command" and argv[-1] == lt._ps_init(150)


@pytest.mark.skipif(os.name != "nt", reason="starts a real powershell.exe")
@pytest.mark.parametrize("policy", ["Restricted", "AllSigned"])
def test_the_startup_script_runs_where_scripts_are_blocked(policy):
    env = dict(os.environ)
    env["PSExecutionPolicyPreference"] = policy  # this process only
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
