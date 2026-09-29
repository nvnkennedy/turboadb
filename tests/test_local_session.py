"""The local shell session (turboadb.gui.local_terminal) as a console stand-in.

Python tools run unbuffered, input goes out LF-only on a writer thread of its
own, every prompt ends with an invisible mark, PowerShell's Read-Host shows its
prompt, typed adb commands reach the device tab's adb server, and Stop ends
only the running command (never the shell, its console or an adb server).
Pure tests run everywhere; the ones marked ``windows_only`` start real shells.
"""
import os
import re
import sys
import threading
import time

import pytest

windows_only = pytest.mark.skipif(os.name != "nt", reason="starts cmd.exe / powershell.exe")


@pytest.fixture
def lt():
    return pytest.importorskip("turboadb.gui.local_terminal")


# ------------------------------------------------------------------ environment

def test_python_tools_run_unbuffered_unless_the_user_chose(lt):
    assert lt.build_shell_env({})["PYTHONUNBUFFERED"] == "1"
    kept = lt.build_shell_env({"pythonunbuffered": ""})  # any spelling, even empty
    assert [k for k in kept if k.upper() == "PYTHONUNBUFFERED"] == ["pythonunbuffered"]
    assert kept["pythonunbuffered"] == ""


def test_cmd_prompt_ends_with_the_mark_once(lt):
    env = lt.build_shell_env({}, mark_prompt=True, system_root=r"C:\Windows")
    assert env["PROMPT"] == "$P$G$E]7717;$P$E\\"
    mine = lt.build_shell_env({"prompt": "$T $P$G"}, mark_prompt=True, system_root=r"C:\Windows")
    assert mine["prompt"] == "$T $P$G$E]7717;$P$E\\"
    again = lt.build_shell_env(mine, mark_prompt=True, system_root=r"C:\Windows")
    assert again["prompt"] == mine["prompt"]  # a TurboADB started from a TurboADB terminal
    assert "PROMPT" not in lt.build_shell_env({}, system_root=r"C:\Windows")
    assert "PROMPT" not in lt.build_shell_env({}, mark_prompt=True, windows=False)


def test_the_prompt_mark_is_what_cmd_prints_for_that_prompt(lt):
    printed = "C:\\w>\x1b]7717;C:\\w\x1b\\"  # $P$G$E]7717;$P$E\
    match = lt.PROMPT_MARK_RE.search(printed)
    assert match.group(1) == "C:\\w" and match.end() == len(printed)
    powershell = "PS C:\\w> \x1b]7717;C:\\w\x07"
    assert lt.PROMPT_MARK_RE.search(powershell).group(1) == "C:\\w"


def test_typed_adb_commands_follow_the_tabs_adb_server(lt):
    base = {"ADB_SERVER_SOCKET": "tcp:10.0.0.9:5037"}
    remote = lt.build_shell_env(base, adb_server_host="192.168.1.50", adb_server_port=5038)
    # adb wraps ADDRESS into tcp:<host>:<port> itself (the v37 bare-host quirk)
    assert remote["ANDROID_ADB_SERVER_ADDRESS"] == "192.168.1.50"
    assert remote["ANDROID_ADB_SERVER_PORT"] == "5038"
    assert "ADB_SERVER_SOCKET" not in remote  # adb would prefer it over ADDRESS/PORT
    default_port = lt.build_shell_env({}, adb_server_host="lab-pc")
    assert default_port["ANDROID_ADB_SERVER_PORT"] == "5037"

    local = lt.build_shell_env(base, adb_server_port=5099)
    assert local["ANDROID_ADB_SERVER_PORT"] == "5099"
    assert "ANDROID_ADB_SERVER_ADDRESS" not in local and "ADB_SERVER_SOCKET" not in local

    plain = lt.build_shell_env(base, adb_server_port=5037)
    assert plain["ADB_SERVER_SOCKET"] == "tcp:10.0.0.9:5037"  # the user's own choice
    assert "ANDROID_ADB_SERVER_PORT" not in plain


def test_powershell_init_script(lt):
    init = lt._ps_init()
    assert init.startswith(lt._PS_UTF8_INIT)
    # everything that calls .NET methods needs FullLanguage
    assert "LanguageMode -eq 'FullLanguage'" in init
    assert "function global:Read-Host" in init and "[Console]::In.ReadLine()" in init
    assert "-AsPlainText" in init  # -AsSecureString still returns a SecureString
    assert "function global:prompt" in init and "']7717;'" in init
    assert '"' not in init  # nothing for the command line to quote or escape
    assert "BufferSize" not in lt._ps_init(80)  # never narrower than the console
    assert "Width -lt 200" in lt._ps_init(200)
    assert "Width -lt 1000" in lt._ps_init(5000)
    argv = lt.shell_argv("powershell", r"C:\Windows", columns=180)
    assert argv[-2] == "-Command" and "Width -lt 180" in argv[-1]


# ------------------------------------------------------------------ input

class _BlockingPipe:
    """A shell's stdin that takes nothing until *release* is set."""

    def __init__(self):
        self.release = threading.Event()
        self.data = []
        self.writing = threading.Event()

    def write(self, view):
        self.writing.set()
        self.release.wait(10)
        self.data.append(bytes(view))
        return len(view)

    def flush(self):
        pass

    def close(self):
        pass


def _session(lt, monkeypatch, stdin=None, shell="cmd"):
    import turboadb.tools as tools

    class FakeProc:
        pid = 4242
        stdout = None

        def __init__(self):
            self.stdin = stdin

        def poll(self):
            return None

    captured = {}

    def popen(argv, **kwargs):
        captured.update(kwargs, argv=argv)
        return FakeProc()

    monkeypatch.setattr(lt.subprocess, "Popen", popen)
    monkeypatch.setattr(lt, "find_adb", lambda *_a, **_k: None)
    monkeypatch.setattr(tools, "find_scrcpy", lambda *_a, **_k: None)
    monkeypatch.setattr(lt, "_get_registry_env", lambda: {})
    monkeypatch.setattr(lt, "_kill_process_tree", lambda proc, wait_s=2.0: None)
    session = lt.LocalShellSession(shell, adb_path="__missing__")
    session.captured = captured
    return session


def _written(pipe, count, timeout=5.0):
    deadline = time.monotonic() + timeout
    while len(pipe.data) < count and time.monotonic() < deadline:
        time.sleep(0.01)
    return pipe.data


def test_input_goes_out_lf_only_in_order(lt, monkeypatch):
    pipe = _BlockingPipe()
    pipe.release.set()
    session = _session(lt, monkeypatch, pipe)
    for data in (b"pause\r\n", b"\r\n", b"y", b"\x03", "échos\r\n", "echo done\r\n"):
        assert session.send(data) is True
    accented = "échos\n".encode(session.input_encoding)
    assert _written(pipe, 6) == [b"pause\n", b"\n", b"y", b"\x03", accented, b"echo done\n"]
    session.close()


def test_cmd_input_is_still_encoded_for_the_console_code_page(lt, monkeypatch):
    pipe = _BlockingPipe()
    pipe.release.set()
    session = _session(lt, monkeypatch, pipe)
    session.input_encoding = "cp850"
    session.send("cd café\r\n".encode("utf-8"))
    assert _written(pipe, 1) == ["cd café\n".encode("cp850")]
    session.close()


def test_input_for_a_program_that_reads_utf8_is_not_re_encoded(lt, monkeypatch):
    """An adb shell typed in CMD reads the input itself and hands every byte
    to the device, which reads UTF-8: ``echo café 中`` reached it as
    ``echo caf\\x82 ?``."""
    pipe = _BlockingPipe()
    pipe.release.set()
    session = _session(lt, monkeypatch, pipe)
    session.input_encoding = "cp850"
    session.utf8_input = True
    session.send("echo café 中\r\n".encode("utf-8"))
    session.send("echo é")
    session.utf8_input = False
    session.send("echo café\r\n".encode("utf-8"))
    assert _written(pipe, 3) == [
        "echo café 中\n".encode("utf-8"),
        "echo é".encode("utf-8"),  # (a str goes the same way)
        "echo café\n".encode("cp850"),
    ]
    session.close()


def test_a_shell_that_does_not_read_never_blocks_the_caller(lt, monkeypatch):
    pipe = _BlockingPipe()  # a command runs and never reads its input
    session = _session(lt, monkeypatch, pipe)
    started = time.monotonic()
    for _ in range(50):
        assert session.send(b"x" * 1000 + b"\r\n") is True
    assert time.monotonic() - started < 1.0  # the UI thread used to hang here
    assert pipe.writing.wait(5)
    monkeypatch.setattr(session, "INPUT_QUEUE_MAX", 60000)
    assert session.send(b"y" * 20000) is False  # bounded: refused, not queued forever
    assert session.discard_pending_input() > 0
    pipe.release.set()
    assert len(_written(pipe, 1)) >= 1
    session.close()


def test_a_closed_session_takes_no_input_and_its_writer_ends(lt, monkeypatch):
    pipe = _BlockingPipe()
    pipe.release.set()
    session = _session(lt, monkeypatch, pipe)
    assert session.send(b"a\n")
    session.close()
    assert session.send(b"b\n") is False
    writer = session._writer
    writer.join(5)
    assert not writer.is_alive()


def test_a_broken_pipe_ends_the_writer_quietly(lt, monkeypatch):
    class Broken(_BlockingPipe):
        def write(self, view):
            raise BrokenPipeError(32, "the shell is gone")

    session = _session(lt, monkeypatch, Broken())
    assert session.send(b"x\n")
    session._writer.join(5)
    assert not session._writer.is_alive()


# ------------------------------------------------------------------ Stop

def test_kill_command_ends_the_commands_and_keeps_the_shell(lt, monkeypatch):
    from turboadb import proctree
    from turboadb.proctree import ProcInfo

    table = {pid: ProcInfo(pid, ppid, name, created) for pid, ppid, name, created in (
        (4242, 1, "cmd.exe", 100.0),
        (11, 4242, "conhost.exe", 101.0),
        (12, 4242, "ping.exe", 102.0),
        (13, 4242, "adb.exe", 103.0),   # `adb logcat` typed in the terminal
        (14, 13, "adb.exe", 104.0),     # the adb server it started
    )}
    ended = []
    monkeypatch.setattr(proctree, "snapshot", lambda: table)
    monkeypatch.setattr(proctree, "_terminate",
                        lambda info, exit_code=1: ended.append((info.pid, exit_code)) or True)
    pipe = _BlockingPipe()  # ping runs: cmd reads nothing meanwhile
    session = _session(lt, monkeypatch, pipe)
    session.send(b"typed during ping\n")
    assert pipe.writing.wait(5)
    session.send(b"typed after that\n")
    assert session._inbox_bytes > 0
    results = []
    done = threading.Event()
    assert session.kill_command(lambda result: (results.append(result), done.set()))
    assert done.wait(5)
    result = results[0]
    assert sorted(result.killed) == [12, 13] and result.foreground
    assert sorted(pid for pid, _code in ended) == [12, 13]  # not the shell, conhost or the server
    assert {code for _pid, code in ended} == {lt.STATUS_CONTROL_C_EXIT}
    assert session.running and session._inbox_bytes == 0  # stale type-ahead went too
    pipe.release.set()
    session.close()


def test_kill_command_reports_an_unreadable_process_table(lt, monkeypatch):
    from turboadb import proctree

    monkeypatch.setattr(proctree, "snapshot", lambda: None)
    assert lt._kill_commands(4242) == (None, False)
    session = _session(lt, monkeypatch, _BlockingPipe())
    session.close()
    assert session.kill_command() is False  # nothing to stop in a closed session


def test_the_tree_kill_spares_the_adb_server_and_falls_back_to_taskkill(lt, monkeypatch):
    from turboadb import proctree

    class Proc:
        pid = 777
        stdin = stdout = None

        def __init__(self):
            self.alive = True

        def poll(self):
            return None if self.alive else 1

        def wait(self, timeout=None):
            self.alive = False
            return 1

        def kill(self):
            self.alive = False

        def terminate(self):
            self.alive = False

    calls = []
    monkeypatch.setattr(proctree, "kill_tree", lambda pid, **kw: calls.append(("tree", pid, kw)) or [pid])
    monkeypatch.setattr(lt.subprocess, "run", lambda argv, **kw: calls.append(("run", argv)))
    lt._kill_process_tree(Proc())
    assert calls == [("tree", 777, {})]  # the default spares a forked adb server

    calls.clear()
    monkeypatch.setattr(proctree, "kill_tree", lambda pid, **kw: None)  # table unreadable
    proc = Proc()
    lt._kill_process_tree(proc)
    if os.name == "nt":
        (_kind, argv), = calls
        assert argv[1:] == ["/PID", "777", "/T", "/F"]
        exe = argv[0].lower()
        assert exe == "taskkill.exe" or exe.endswith(os.sep + "system32" + os.sep + "taskkill.exe")
    else:
        assert calls == [] and not proc.alive  # terminate()


def test_proctree_passes_the_exit_code(monkeypatch):
    from turboadb import proctree
    from turboadb.proctree import ProcInfo

    table = {10: ProcInfo(10, 1, "cmd.exe", 1.0), 11: ProcInfo(11, 10, "ping.exe", 2.0)}
    codes = []
    monkeypatch.setattr(proctree, "_terminate", lambda info, exit_code=1: codes.append(exit_code) or True)
    proctree.kill_tree(10, include_root=False, procs=table, exit_code=0xC000013A)
    proctree.kill_tree(10, include_root=False, procs=table)
    assert codes == [0xC000013A, 1]


# ------------------------------------------------------------------ grep probe

def test_no_grep_on_path_is_not_gnu_grep(lt, tmp_path):
    assert lt.gnu_grep({"PATH": str(tmp_path)}) is False
    assert lt.gnu_grep({}) is False


@windows_only
def test_gnu_grep_is_recognised_by_its_version(lt, tmp_path):
    (tmp_path / "grep.bat").write_text("@echo grep (GNU grep) 3.11\r\n")
    env = dict(os.environ, PATH=str(tmp_path))
    assert lt.gnu_grep(env) is True
    (tmp_path / "grep.bat").write_text("@echo BusyBox v1.36 multi-call binary\r\n")
    assert lt.gnu_grep(env) is False


# ------------------------------------------------------------------ real shells

def _read_until(session, pattern, timeout=30.0, out=""):
    from turboadb.results import strip_ansi

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        chunk = session.read(65536)
        if chunk:
            out += chunk.decode("utf-8", "replace")
            if re.search(pattern, strip_ansi(out)):
                return out
        else:
            time.sleep(0.02)
    raise AssertionError(f"{pattern!r} never came: {out[-1500:]!r}")


def _marks(lt, text):
    return lt.PROMPT_MARK_RE.findall(text)


@windows_only
@pytest.mark.parametrize("shell", ["cmd", "powershell"])
def test_real_prompt_mark_and_stop_that_keeps_the_shell(lt, tmp_path, shell):
    session = lt.LocalShellSession(shell, cwd=str(tmp_path), adb_path="__missing__")
    try:
        out = _read_until(session, r">\s*$")
        assert _marks(lt, out)[-1] == str(tmp_path)  # the prompt's mark names the folder
        session.send("set TADBV=kept-7\r\n" if shell == "cmd" else "$tadbv = 'kept-7'\r\n")
        _read_until(session, r">\s*$", out="")
        session.send("ping -t 127.0.0.1\r\n")
        _read_until(session, r"Reply from.*\n.*Reply from")
        pid = session.proc.pid
        done = threading.Event()
        results = []
        assert session.kill_command(lambda result: (results.append(result), done.set()))
        assert done.wait(10) and results[0].killed
        tail = _read_until(session, r">\s*$", out="")
        if shell == "cmd":
            assert "^C" in tail  # cmd's own, for a command ended as by Ctrl+C
        assert session.running and session.proc.pid == pid
        session.send("echo %TADBV%\r\n" if shell == "cmd" else "echo $tadbv\r\n")
        _read_until(session, r"kept-7")
    finally:
        session.close()


@windows_only
def test_real_pause_takes_one_lf_and_prints_one_prompt(lt, tmp_path):
    session = lt.LocalShellSession("cmd", cwd=str(tmp_path), adb_path="__missing__")
    try:
        _read_until(session, r">\s*$")
        session.send("pause\r\n")
        _read_until(session, r"continue \. \. \. ")
        session.send("\r\n")  # CR LF used to leave its LF behind for a second prompt
        out = _read_until(session, r">\s*$", out="")
        time.sleep(0.8)
        out += session.read(65536).decode("utf-8", "replace")
        assert len(_marks(lt, out)) == 1, out
    finally:
        session.close()


@windows_only
def test_real_read_host_shows_its_prompt_and_does_not_echo(lt, tmp_path):
    session = lt.LocalShellSession("powershell", cwd=str(tmp_path), adb_path="__missing__")
    try:
        _read_until(session, r">\s*$", timeout=60)
        session.send("$n = Read-Host 'Your name'; \"got [$n]\"\r\n")
        _read_until(session, r"Your name: $")
        session.send("bob\r\n")
        out = _read_until(session, r"got \[bob\]", out="")
        assert "bob" not in out.split("got [")[0]  # Read-Host no longer echoes the answer
        session.send("$p = Read-Host -AsSecureString 'Pin'; $p.GetType().Name\r\n")
        _read_until(session, r"Pin: $")
        session.send("1234\r\n")
        _read_until(session, r"SecureString", out="")
    finally:
        session.close()


@windows_only
def test_real_python_output_streams(lt, tmp_path):
    script = tmp_path / "tick.py"
    script.write_text("import time\nfor i in range(3):\n    print('tick', i)\n    time.sleep(0.8)\n")
    session = lt.LocalShellSession("cmd", cwd=str(tmp_path), adb_path="__missing__")
    try:
        _read_until(session, r">\s*$")
        session.send(f'"{sys.executable}" "{script}"\r\n')
        started = time.monotonic()
        _read_until(session, r"tick 0", timeout=10, out="")
        first = time.monotonic() - started
        _read_until(session, r"tick 2", timeout=10, out="")
        # the first line came long before the script ended (it held all of
        # its output until exit over a pipe)
        assert first < 1.2, first
    finally:
        session.close()
