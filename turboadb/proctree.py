"""Find and end a process's descendants without taking down an adb server.

adb on Windows launches its server as a child of whichever adb client found no
server running, and that client stays the server's parent while it lives.  A
plain ``taskkill /T`` of a terminal (or of scrcpy, which runs its own adb
clients) therefore also killed the shared adb server, and every device tab lost
its shell and logcat at once.  :func:`kill_tree` walks the tree itself and
leaves such a server, and everything under it, alone.

:func:`stop_process` is the one way a child TurboADB started is stopped (the
engine's adb children, scrcpy, the GUI's capture and probe streams), so a change
to how children end is made in one place.

Windows uses a Toolhelp32 snapshot through ctypes (no third-party packages),
and guards against PID reuse by comparing creation times.  POSIX reads ``ps``.
Every function returns ``None`` (or an empty result) instead of raising, so a
caller can fall back to its old method.
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
from typing import Callable, Dict, Iterable, List, NamedTuple, Optional

__all__ = [
    "ProcInfo",
    "CONSOLE_LOCK",
    "snapshot",
    "descendants",
    "is_adb_server",
    "kill_tree",
    "stop_process",
]

_ADB_NAMES = ("adb", "adb.exe")

# AttachConsole and FreeConsole act on the whole process, which is attached to
# one console at a time, and so does the flag that ignores Ctrl+C.  Everything
# that attaches to another process's console to raise an event there (scrcpy's
# clean stop, a local terminal's Stop) or sets that flag while it starts a
# child (a local shell) holds this one lock: with a lock each, one attach was
# refused while the other held the console, or freed the console under it.
CONSOLE_LOCK = threading.Lock()


class ProcInfo(NamedTuple):
    pid: int
    ppid: int
    name: str        # executable base name, lower-case ("adb.exe", "cmd.exe")
    created: float   # creation time in seconds on an arbitrary clock; 0.0 = unknown


# --------------------------------------------------------------------------- #
# Snapshots
# --------------------------------------------------------------------------- #
if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    _TH32CS_SNAPPROCESS = 0x00000002
    _PROCESS_TERMINATE = 0x0001
    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _INVALID_HANDLE = ctypes.c_void_p(-1).value

    class _PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", ctypes.c_wchar * 260),
        ]

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
    _k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    _k32.Process32FirstW.argtypes = (wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W))
    _k32.Process32FirstW.restype = wintypes.BOOL
    _k32.Process32NextW.argtypes = (wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W))
    _k32.Process32NextW.restype = wintypes.BOOL
    _k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    _k32.OpenProcess.restype = wintypes.HANDLE
    _k32.GetProcessTimes.argtypes = (wintypes.HANDLE,) + (ctypes.POINTER(wintypes.FILETIME),) * 4
    _k32.GetProcessTimes.restype = wintypes.BOOL
    _k32.TerminateProcess.argtypes = (wintypes.HANDLE, wintypes.UINT)
    _k32.TerminateProcess.restype = wintypes.BOOL
    _k32.CloseHandle.argtypes = (wintypes.HANDLE,)
    _k32.CloseHandle.restype = wintypes.BOOL

    def _creation_time(handle) -> float:
        times = [wintypes.FILETIME() for _ in range(4)]
        if not _k32.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
            return 0.0
        ft = times[0]
        return ((ft.dwHighDateTime << 32) | ft.dwLowDateTime) / 1e7

    def _created(pid: int) -> float:
        handle = _k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return 0.0
        try:
            return _creation_time(handle)
        finally:
            _k32.CloseHandle(handle)

    def _snapshot_raw() -> Optional[List[tuple]]:
        snap = _k32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
        if not snap or snap == _INVALID_HANDLE:
            return None
        try:
            entry = _PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
            rows = []
            ok = _k32.Process32FirstW(snap, ctypes.byref(entry))
            while ok:
                rows.append((int(entry.th32ProcessID), int(entry.th32ParentProcessID),
                             entry.szExeFile.lower()))
                ok = _k32.Process32NextW(snap, ctypes.byref(entry))
            return rows
        finally:
            _k32.CloseHandle(snap)

    def _terminate(info: ProcInfo, exit_code: int = 1) -> bool:
        handle = _k32.OpenProcess(
            _PROCESS_TERMINATE | _PROCESS_QUERY_LIMITED_INFORMATION, False, info.pid)
        if not handle:
            return False
        try:
            # The PID may have been reused since the snapshot: only end the
            # process that was actually seen in the tree.
            if info.created and abs(_creation_time(handle) - info.created) > 1e-3:
                return False
            return bool(_k32.TerminateProcess(handle, exit_code))
        finally:
            _k32.CloseHandle(handle)

else:

    def _created(pid: int) -> float:
        return 0.0

    def _snapshot_raw() -> Optional[List[tuple]]:
        try:
            out = subprocess.run(
                ["ps", "-A", "-o", "pid=", "-o", "ppid=", "-o", "comm="],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, timeout=5, check=False,
            ).stdout.decode("utf-8", "replace")
        except (OSError, subprocess.SubprocessError):
            return None
        rows = []
        for line in out.splitlines():
            parts = line.split(None, 2)
            if len(parts) < 3 or not parts[0].isdigit() or not parts[1].isdigit():
                continue
            rows.append((int(parts[0]), int(parts[1]), os.path.basename(parts[2]).lower()))
        return rows

    def _terminate(info: ProcInfo, exit_code: int = 1) -> bool:
        try:
            os.kill(info.pid, signal.SIGKILL)
            return True
        except OSError:
            return False


def snapshot() -> Optional[Dict[int, ProcInfo]]:
    """Every running process by PID, or ``None`` when the table can't be read.

    Creation times are filled in lazily by :func:`descendants` (opening every
    process on the machine would be wasted work)."""
    try:
        rows = _snapshot_raw()
    except Exception:
        return None
    if rows is None:
        return None
    return {pid: ProcInfo(pid, ppid, name, 0.0) for pid, ppid, name in rows}


def _with_time(info: ProcInfo) -> ProcInfo:
    if info.created or os.name != "nt":
        return info
    return info._replace(created=_created(info.pid))


def descendants(pid: int, procs: Optional[Dict[int, ProcInfo]] = None) -> List[ProcInfo]:
    """*pid*'s descendants, parents before their children.

    A process whose recorded parent PID matches but which is OLDER than that
    parent is an orphan of an earlier process that had the same PID, and is
    not part of the tree."""
    if procs is None:
        procs = snapshot()
    if not procs:
        return []
    kids: Dict[int, List[ProcInfo]] = {}
    for info in procs.values():
        if info.pid != info.ppid:
            kids.setdefault(info.ppid, []).append(info)
    root = procs.get(pid)
    root = _with_time(root) if root is not None else ProcInfo(pid, 0, "", _created(pid))
    out: List[ProcInfo] = []
    stack = [root]
    seen = {pid}
    while stack:
        parent = stack.pop()
        for child in kids.get(parent.pid, ()):
            if child.pid in seen:
                continue
            child = _with_time(child)
            if parent.created and child.created and child.created + 1e-3 < parent.created:
                continue
            seen.add(child.pid)
            out.append(child)
            stack.append(child)
    return out


def is_adb_server(info: ProcInfo, procs: Dict[int, ProcInfo]) -> bool:
    """True for an adb server: an adb whose parent is an adb client.

    The client forks ``adb fork-server server`` (Windows spawns it as a child),
    so an adb under an adb is the daemon every tool shares."""
    if info.name not in _ADB_NAMES:
        return False
    parent = procs.get(info.ppid)
    return parent is not None and parent.name in _ADB_NAMES


def kill_tree(
    pid: int,
    *,
    include_root: bool = True,
    spare: Optional[Callable[[ProcInfo, Dict[int, ProcInfo]], bool]] = is_adb_server,
    skip_names: Iterable[str] = (),
    procs: Optional[Dict[int, ProcInfo]] = None,
    terminate: Optional[Callable[[ProcInfo], bool]] = None,
    exit_code: int = 1,
) -> Optional[List[int]]:
    """End *pid*'s descendants (deepest first) and, with *include_root*, *pid*.

    A process for which *spare* returns True is left running together with its
    own subtree; by default that is an adb server a client in the tree started.
    *skip_names* are executable names to leave alone as well (for example
    ``conhost.exe`` when only a shell's commands should end, not the shell's
    console).  *exit_code* is the status the ended processes report on
    Windows.  Returns the PIDs that were ended, or ``None`` when the process
    table could not be read, so the caller can fall back to ``taskkill /T``.
    """
    if procs is None:
        procs = snapshot()
    if procs is None:
        return None
    end = terminate or (_terminate if exit_code == 1 else lambda info: _terminate(info, exit_code))
    skip = {n.lower() for n in skip_names}
    doomed: List[ProcInfo] = []
    spared = set()
    for info in descendants(pid, procs):
        if info.ppid in spared or info.name in skip or (spare and spare(info, procs)):
            spared.add(info.pid)
            continue
        doomed.append(info)
    if include_root:
        root = procs.get(pid)
        doomed.insert(0, _with_time(root) if root is not None
                      else ProcInfo(pid, 0, "", _created(pid)))
    killed = []
    for info in reversed(doomed):   # children before parents
        try:
            if end(info):
                killed.append(info.pid)
        except Exception:
            pass
    return killed


# --------------------------------------------------------------------------- #
# Stopping a child process
# --------------------------------------------------------------------------- #
def stop_process(proc, *, grace: float = 2.0, close_pipes: bool = False,
                 reap: bool = True, tree: bool = False) -> Optional[int]:
    """End a child process and reap it: terminate, wait up to *grace* seconds,
    then kill.  Never raises; returns the exit code, or None when the process
    was not reaped.

    *grace* 0 kills at once.  With *close_pipes* its stdin, stdout and stderr
    are closed as well, so a thread blocked reading them returns at once
    (leave them open while your own reader still needs them).  *reap* False
    never waits, for a caller that must not block (the UI thread) while its
    own reader thread reaps the process.  With *tree*, a process that has to
    be killed takes its descendants with it (:func:`kill_tree`, which spares
    an adb server): scrcpy runs adb clients that outlived it otherwise.  Only
    a real child process is walked, as a made-up PID could be anybody's."""
    if grace > 0:
        try:
            if proc.poll() is None:
                proc.terminate()
        except Exception:
            pass
    else:
        _kill(proc, tree)
    if close_pipes:
        for pipe in (getattr(proc, "stdin", None), getattr(proc, "stdout", None),
                     getattr(proc, "stderr", None)):
            if pipe is not None:
                try:
                    pipe.close()
                except Exception:
                    pass
    if not reap:
        return None
    if grace > 0:
        try:
            return proc.wait(timeout=grace)
        except Exception:
            pass
        _kill(proc, tree)
    try:
        return proc.wait(timeout=1.0)
    except Exception:
        return None


def _kill(proc, tree: bool) -> None:
    """Kill *proc*, and with *tree* its descendants while it still runs (a
    reaped process's PID may already be somebody else's).  Popen.kill itself
    leaves a process that has ended alone."""
    if tree and isinstance(proc, subprocess.Popen):
        try:
            if proc.poll() is None:
                kill_tree(proc.pid)  # its children first, the process itself last
        except Exception:
            pass
    try:
        proc.kill()
    except Exception:
        pass
