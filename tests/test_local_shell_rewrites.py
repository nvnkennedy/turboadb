"""Local PowerShell / CMD command rewrites for commands that misbehave on a
piped console: PowerShell's ``where`` alias and bare interactive interpreters."""
import types

import pytest

pytest.importorskip("PyQt5")


def _rewrite(shell_type, line):
    from turboadb.gui.device_tab import _LocalShellWidget

    fake = types.SimpleNamespace(
        shell_type=shell_type,
        _INTERACTIVE_FLAGS=_LocalShellWidget._INTERACTIVE_FLAGS,
        _NON_REPL_FLAGS=_LocalShellWidget._NON_REPL_FLAGS,
    )
    return _LocalShellWidget._interactive_rewrite(fake, line)


def test_powershell_where_runs_where_exe_outside_pipelines():
    assert _rewrite("powershell", "where python") == "where.exe python"
    assert _rewrite("powershell", "where") == "where.exe"
    assert _rewrite("powershell", "WHERE /R C:\\tools adb") == "where.exe /R C:\\tools adb"
    # Where-Object in a pipeline or with a script block stays PowerShell's.
    assert _rewrite("powershell", "Get-Process | where CPU -gt 10") is None
    assert _rewrite("powershell", "where { $_.CPU -gt 10 }") is None
    # CMD's where is already where.exe.
    assert _rewrite("cmd", "where python") is None


def test_bare_interpreters_start_their_interactive_prompt():
    for shell in ("powershell", "cmd"):
        assert _rewrite(shell, "python") == "python -i -u"
        assert _rewrite(shell, "py -3.11") == "py -3.11 -i -u"
        assert _rewrite(shell, "python.exe -X utf8") == "python.exe -X utf8 -i -u"
        assert _rewrite(shell, "node") == "node -i"


def test_welcome_banner_is_two_lines_in_a_frame():
    from turboadb.gui.device_tab import _boxed_banner, _str_width

    first = "\x1b[1;92m● Connected\x1b[0m to \x1b[1;35mvivo V2318\x1b[0m [USB]"
    second = "Phone  ·  Android 16 · SDK 36  ·  arm64-v8a"
    rows = [row for row in _boxed_banner([first, second]).splitlines() if row]
    assert len(rows) == 4  # top edge, two text lines, bottom edge
    assert "┌" in rows[0] and "└" in rows[-1]
    # Every row has the same display width, so the right edge lines up.
    assert len({_str_width(row) for row in rows}) == 1


def test_scripts_and_explicit_modes_run_as_typed():
    for line in (
        "python script.py",
        "python -c \"print(1)\"",
        "python -m pip list",
        "python --version",
        "python -i",
        "node app.js",
        "node -e \"console.log(1)\"",
        "pip list",
        "",
    ):
        assert _rewrite("powershell", line) is None, line
