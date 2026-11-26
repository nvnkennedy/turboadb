"""A fake adb for the tests of adb clients (6.5.0): a Python script behind an
``adb.exe`` launcher on Windows (pip's console-script launcher, the one every
``pip install`` puts in Scripts), a script with a ``#!`` line elsewhere.

What it does is in a scenario file (JSON) the test writes; its path is in the
``TURBOADB_FAKE_ADB`` environment variable, which the clients inherit.  A
scenario is a list of rules; the first whose ``match`` is in the command (the
arguments after adb's own options: ``shell getprop x``) answers::

    {"match": "shell hold", "sleep": 60}                 # hangs (until ended)
    {"match": "devices", "answers": [{"stdout": "…"}, …]} # one per call, the last repeats
    {"match": "print", "stdout_hex": "5a65…", "exit": 0}
    {"match": "start-server", "server": true}            # starts a detached "server"

An answer may have ``stdout``, ``stdout_hex``, ``stderr``, ``stderr_hex``,
``exit``, ``sleep`` (before it exits) and ``server``.  Every call is recorded
in ``calls.jsonl`` beside the scenario: its pid, its parent's (on Windows the
launcher's, which is the client TurboADB started) and its command.  A started
server writes ``server.pid`` and lives for a minute.
"""

from __future__ import annotations

import io
import json
import os
import pkgutil
import platform
import stat
import subprocess
import sys
import time
import zipfile

SCRIPT = r'''
import json, os, subprocess, sys, time

SERVER = "import sys, time\nopen(sys.argv[1], 'w').write(str(__import__('os').getpid()))\ntime.sleep(60)\n"


def _lock(path):
    for _ in range(400):
        try:
            return os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            time.sleep(0.005)
    return None


def main():
    scenario_path = os.environ["TURBOADB_FAKE_ADB"]
    folder = os.path.dirname(scenario_path)
    args = sys.argv[1:]
    while args and args[0] in ("-s", "-H", "-P", "-L", "-t"):
        args = args[2:]
    while args and args[0] in ("-a", "-d", "-e"):
        args = args[1:]
    command = " ".join(args)
    with open(os.path.join(folder, "calls.jsonl"), "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"pid": os.getpid(), "ppid": os.getppid(), "command": command}) + "\n")
    with open(scenario_path, encoding="utf-8") as fh:
        rules = json.load(fh)
    index, rule = next(((i, r) for i, r in enumerate(rules) if r.get("match", "") in command),
                       (-1, {}))
    answers = rule.get("answers")
    if answers:
        lock = os.path.join(folder, "counts.lock")
        held = _lock(lock)
        try:
            counts_path = os.path.join(folder, "counts.json")
            try:
                with open(counts_path, encoding="utf-8") as fh:
                    counts = json.load(fh)
            except (OSError, ValueError):
                counts = {}
            n = counts.get(str(index), 0)
            counts[str(index)] = n + 1
            with open(counts_path, "w", encoding="utf-8") as fh:
                json.dump(counts, fh)
        finally:
            if held is not None:
                os.close(held)
                os.remove(lock)
        rule = dict(rule, **answers[min(n, len(answers) - 1)])
    if rule.get("server"):
        flags = 0x00000008 | 0x00000200 if os.name == "nt" else 0  # DETACHED | NEW_GROUP
        subprocess.Popen([sys.executable, "-c", SERVER, os.path.join(folder, "server.pid")],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, creationflags=flags,
                         start_new_session=os.name != "nt")
    out = sys.stdout.buffer
    if "stdout_hex" in rule:
        out.write(bytes.fromhex(rule["stdout_hex"]))
    if "stdout" in rule:
        out.write(rule["stdout"].encode("utf-8"))
    out.flush()
    err = sys.stderr.buffer
    if "stderr_hex" in rule:
        err.write(bytes.fromhex(rule["stderr_hex"]))
    if "stderr" in rule:
        err.write(rule["stderr"].encode("utf-8"))
    err.flush()
    if rule.get("sleep"):
        time.sleep(float(rule["sleep"]))
    sys.exit(int(rule.get("exit", 0)))


main()
'''


def _launcher() -> bytes:
    """pip's console-script launcher for this machine, or b"" without one."""
    name = "t64-arm.exe" if platform.machine().lower() in ("arm64", "aarch64") else "t64.exe"
    for package in ("pip._vendor.distlib", "distlib"):
        try:
            data = pkgutil.get_data(package, name)
        except (ImportError, OSError):
            data = None
        if data:
            return data
    return b""


def python() -> str:
    """The interpreter the fake runs on: the base one in a virtual
    environment (its python.exe is a launcher of its own)."""
    return getattr(sys, "_base_executable", None) or sys.executable


def build(folder) -> str:
    """Write the fake adb into *folder*; returns its path.  Raises
    RuntimeError where no launcher can be made (Windows without pip)."""
    os.makedirs(folder, exist_ok=True)
    if os.name == "nt":
        launcher = _launcher()
        if not launcher:
            raise RuntimeError("pip's launcher (distlib t64.exe) is not available")
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("__main__.py", SCRIPT)
        path = os.path.join(folder, "adb.exe")
        with open(path, "wb") as fh:
            fh.write(launcher + ('#!"%s"\n' % python()).encode("utf-8") + archive.getvalue())
        return path
    path = os.path.join(folder, "adb")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(f"#!{python()}\n{SCRIPT}")
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


class FakeAdb:
    """The fake adb a test drives: :meth:`play` a scenario, read :meth:`calls`."""

    def __init__(self, folder):
        self.folder = str(folder)
        self.path = build(os.path.join(self.folder, "bin"))
        self.scenario = os.path.join(self.folder, "scenario.json")
        self.play([])

    def play(self, rules) -> "FakeAdb":
        with open(self.scenario, "w", encoding="utf-8") as fh:
            json.dump(list(rules), fh)
        for name in ("counts.json", "calls.jsonl", "server.pid"):
            try:
                os.remove(os.path.join(self.folder, name))
            except OSError:
                pass
        return self

    def calls(self) -> list:
        try:
            with open(os.path.join(self.folder, "calls.jsonl"), encoding="utf-8") as fh:
                return [json.loads(line) for line in fh if line.strip()]
        except OSError:
            return []

    def wait_call(self, text, timeout=15.0):
        """The first call whose command has *text*, once it has started."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            for call in self.calls():
                if text in call["command"]:
                    return call
            time.sleep(0.02)
        raise AssertionError(f"adb {text!r} was never called: {self.calls()}")

    def server_pid(self, timeout=15.0):
        path = os.path.join(self.folder, "server.pid")
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                with open(path, encoding="utf-8") as fh:
                    text = fh.read().strip()
                if text:
                    return int(text)
            except OSError:
                pass
            time.sleep(0.02)
        raise AssertionError("the fake adb server never started")


def alive(pid) -> bool:
    """Whether the process *pid* still runs."""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = kernel.OpenProcess(0x1000, False, int(pid))  # QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # a zombie (ended, not reaped) is not running
    try:
        with open(f"/proc/{int(pid)}/stat", encoding="utf-8") as fh:
            return fh.read().split(")")[-1].split()[0] != "Z"
    except OSError:
        return True


def gone(pid, timeout=10.0) -> bool:
    """Wait up to *timeout* for *pid* to end; whether it did."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if not alive(pid):
            return True
        time.sleep(0.05)
    return not alive(pid)


def kill(pid) -> None:
    """End *pid* (a fake server left behind by a test)."""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/PID", str(int(pid))], capture_output=True,
                           creationflags=0x08000000)
        else:
            os.kill(int(pid), 9)
    except (OSError, ValueError):
        pass
