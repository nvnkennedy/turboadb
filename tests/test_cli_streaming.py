"""`turboadb shell -- CMD` and `turboadb adb -- ARGS` run the way adb itself
does: attached to the console, so a ping, top or logcat shows its lines as they
come instead of all at once at the end — and after 60 s not at all, when the
command timeout threw everything away. `--json` still collects the output into
its one document, and `--timeout 0` means no limit anywhere."""
import json
import os
import subprocess
import sys
import time

import pytest

import turboadb.cli as cli
from turboadb.config import ADBConfig
from turboadb.core import ADBHandler
from turboadb.exceptions import ADBTimeoutError
from turboadb.results import CommandResult

from test_cli_commands import FakeDev

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture
def run(monkeypatch, capsys):
    monkeypatch.setattr(cli, "adb_available", lambda *a, **k: True)
    monkeypatch.setattr(cli, "scrcpy_available", lambda *a, **k: True)

    def go(argv, dev):
        monkeypatch.setattr(cli, "_handler", lambda args, **kw: dev)
        rc = cli.main(argv)
        out, err = capsys.readouterr()
        return rc, out, err

    return go


def _fake_adb(tmp_path, script: str) -> str:
    """An adb executable that runs the Python *script* with adb's arguments."""
    body = tmp_path / "fake_adb.py"
    body.write_text(script, encoding="utf-8")
    if os.name == "nt":
        exe = tmp_path / "adb.cmd"
        exe.write_text(f'@"{sys.executable}" "{body}" %*\r\n', encoding="utf-8")
    else:
        exe = tmp_path / "adb"
        exe.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{body}" "$@"\n', encoding="utf-8")
        exe.chmod(0o755)
    return str(exe)


def test_shell_output_arrives_while_the_command_runs(tmp_path):
    adb = _fake_adb(tmp_path, (
        "import sys, time\n"
        "print('ARGV ' + ' | '.join(sys.argv[1:]), flush=True)\n"
        "for i in range(3):\n"
        "    print('reply %d' % i, flush=True)\n"
        "    time.sleep(0.4)\n"
        "sys.exit(3)\n"
    ))
    child = (
        "import sys\n"
        f"sys.path.insert(0, {REPO!r})\n"
        "import turboadb.cli as cli\n"
        "from turboadb.core import ADBHandler\n"
        "ADBHandler.connect = lambda self, **kw: self\n"
        f"sys.exit(cli.main(['-s', 'X', '--adb-path', {adb!r}, 'shell', '--', 'ping', '8.8.8.8']))\n"
    )
    env = dict(os.environ, TURBOADB_AUTO_FETCH="0")
    proc = subprocess.Popen([sys.executable, "-c", child], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, env=env)
    try:
        first = proc.stdout.readline()
        second = proc.stdout.readline()
        second_at = time.monotonic()
        rest = proc.stdout.read()
        ended_at = time.monotonic()
        assert proc.wait(timeout=30) == 3  # the device command's own exit code
    finally:
        if proc.poll() is None:
            proc.kill()
    assert first.strip() == b"ARGV -s | X | shell | ping 8.8.8.8"
    assert second.strip() == b"reply 0" and rest.split() == [b"reply", b"1", b"reply", b"2"]
    # collected output would only arrive at exit, all at once
    assert ended_at - second_at > 0.5


def test_run_attached_gives_adb_the_console_and_its_exit_code(capfd):
    dev = ADBHandler(ADBConfig(adb_path=sys.executable))  # "adb" is python here
    code = dev.run_attached(["-c", "import sys; print('from the child'); sys.exit(4)"])
    assert code == 4
    assert "from the child" in capfd.readouterr().out


def test_run_attached_timeout_ends_adb_and_keeps_what_it_showed(capfd):
    dev = ADBHandler(ADBConfig(adb_path=sys.executable))
    started = time.monotonic()
    with pytest.raises(ADBTimeoutError, match="timed out after 1s"):
        dev.run_attached(
            ["-c", "import time; print('shown first', flush=True); time.sleep(20)"], timeout=1
        )
    assert time.monotonic() - started < 15  # killed, not waited out
    assert "shown first" in capfd.readouterr().out


def test_interactive_shell_runs_a_command_as_root_the_way_shell_does(fake_adb, monkeypatch):
    calls = []
    monkeypatch.setattr("turboadb.core.subprocess.call", lambda cmd, **kw: calls.append(cmd) or 0)
    fake_adb.add("@@su=", stdout="@@su=c\n")  # this device's su takes `su -c`
    dev = ADBHandler(ADBConfig(serial="S1"))
    dev.shell("cat /data/x", su=True)
    ran = next(argv for argv in reversed(fake_adb.calls) if any("cat /data/x" in a for a in argv))
    assert dev.interactive_shell("cat /data/x", su=True) == 0
    assert calls[0][1:] == ran[1:]  # the very same adb command line
    assert calls[0][-2] == "shell" and "su -c 'cat /data/x'" in calls[0][-1]
    dev.interactive_shell()
    assert calls[1][1:] == ["-s", "S1", "shell"]  # no command: the interactive shell


def test_shell_and_adb_stream_unless_json(run):
    dev = FakeDev(interactive_shell=0)
    assert run(["shell", "--", "ping", "8.8.8.8"], dev)[0] == 0
    assert dev.called("interactive_shell") == [(("ping 8.8.8.8",), {"su": False, "timeout": None})]
    assert not dev.called("shell")

    dev = FakeDev(interactive_shell=2)
    assert run(["--timeout", "30", "shell", "--su", "--", "logcat"], dev)[0] == 2
    assert dev.called("interactive_shell") == [(("logcat",), {"su": True, "timeout": 30.0})]

    dev = FakeDev(interactive_shell=0)
    run(["shell", "--timeout", "0", "--", "top"], dev)  # 0: no limit
    assert dev.called("interactive_shell")[0][1]["timeout"] is None

    dev = FakeDev(shell=CommandResult("shell echo hello", 0, "hello\n", "", 0.1))
    rc, out, _ = run(["shell", "--json", "--", "echo", "hello"], dev)
    assert rc == 0 and json.loads(out)["stdout"] == "hello\n"
    assert not dev.called("interactive_shell")

    dev = FakeDev(run_attached=0)
    assert run(["--timeout", "5", "adb", "--", "logcat"], dev)[0] == 0
    assert dev.called("run_attached") == [((["logcat"],), {"timeout": 5.0})]


def test_timeout_zero_lifts_every_limit_and_bad_values_are_refused(capsys):
    parser = cli.build_parser()
    cfg = cli._config(parser.parse_args(["push", "a", "b", "--timeout", "0"]))
    assert cfg.command_timeout is None and cfg.transfer_timeout is None
    assert cli._config(parser.parse_args(["info"])).command_timeout == 60.0  # default kept
    for bad in ("-1", "inf", "soon"):
        with pytest.raises(SystemExit) as exc:
            parser.parse_args(["info", "--timeout", bad])
        assert exc.value.code == 2
    assert "--timeout" in capsys.readouterr().err


def test_bugreport_timeout_zero_is_no_limit(run):
    dev = FakeDev(bugreport="br.zip")
    assert run(["bugreport", "br.zip", "--timeout", "0"], dev)[0] == 0
    assert dev.called("bugreport") == [(("br.zip",), {"timeout": None})]
    dev = FakeDev(bugreport="br.zip")
    run(["bugreport", "br.zip"], dev)
    assert dev.called("bugreport") == [(("br.zip",), {})]  # the engine's own default
