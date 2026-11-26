"""How TurboADB starts, waits for and ends an adb client.

Every adb *client* TurboADB starts goes through :func:`popen`.  On Windows it
runs in a job object of this process (:class:`_Job`) whose limits are
``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`` and ``JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK``:

* the client is started suspended, put into the job, then resumed, so it is
  in the job before it runs a single instruction;
* when this process ends, however it ends (a crash too: Windows closes its
  handles), the job closes and every adb client still running ends with it;
  an ``adb.exe`` used to outlive a crashed host program;
* what a client starts itself leaves the job silently: the adb *server* a
  client starts when none runs is shared with every other tool on the PC and
  is never ended with it.

:func:`run` is the one wait for a client that is run to its end: it looks at
*cancel* (a ``threading.Event`` or a callable returning a bool) every
:data:`POLL_S` and at its time limit, and ends the client (and only it) at
once when either says so.  :func:`decode` reads adb's output as text without
ever failing: a line that is valid UTF-8 is UTF-8, else it is in the OEM code
page (adb's own messages and Windows' around them are, on a German Windows
cp850).

Elsewhere (Linux, macOS) the client is a plain child process.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from typing import Callable, NamedTuple, Optional

# The exit code of a client that TurboADB ended: it ran past its time limit,
# or its call was cancelled (as GNU timeout reports a timeout).
TIMEOUT_EXIT_CODE = 124
# How often a waiting call looks at its cancel and its time limit.
POLL_S = 0.1

_WINDOWS = os.name == "nt"
NO_WINDOW = 0x08000000 if _WINDOWS else 0  # CREATE_NO_WINDOW
_CREATE_SUSPENDED = 0x00000004
_KILL_ON_JOB_CLOSE = 0x00002000
# SILENT_BREAKAWAY_OK, and BREAKAWAY_OK: a child that asks to leave may too
_BREAKAWAY = 0x00001000 | 0x00000800
# How long the output of an ended client is still read: something the client
# started may hold its pipes (a server started with inherited handles).
_DRAIN_S = 5.0


def cancel_check(cancel) -> Optional[Callable[[], bool]]:
    """*cancel* as a callable that says whether to stop: a ``threading.Event``
    (or anything with ``is_set``), a callable returning a bool, or None (never)."""
    if cancel is None:
        return None
    is_set = getattr(cancel, "is_set", None)
    if callable(is_set):
        return is_set
    if callable(cancel):
        return lambda: bool(cancel())
    raise TypeError(f"cancel must be a threading.Event or a callable, not {type(cancel).__name__}")


# --------------------------------------------------------------------------- #
# The job (Windows)
# --------------------------------------------------------------------------- #
class _Job:
    """This process's kill-on-close job for adb clients: created on first use
    and held until the process ends (then Windows closes it, and every client
    still in it ends)."""

    _lock = threading.Lock()
    _api = None
    _handle = None
    _failed = False

    @classmethod
    def api(cls):
        if cls._api is None:
            import ctypes
            from ctypes import wintypes

            kernel = ctypes.WinDLL("kernel32", use_last_error=True)  # (own argtypes)
            handle, dword, bool_ = wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL
            for name, args, result in (
                ("CreateJobObjectW", (ctypes.c_void_p, wintypes.LPCWSTR), handle),
                ("SetInformationJobObject", (handle, ctypes.c_int, ctypes.c_void_p, dword), bool_),
                ("AssignProcessToJobObject", (handle, handle), bool_),
                ("IsProcessInJob", (handle, handle, ctypes.POINTER(bool_)), bool_),
                ("OpenProcess", (dword, bool_, dword), handle),
                ("OpenThread", (dword, bool_, dword), handle),
                ("ResumeThread", (handle,), dword),
                ("CreateToolhelp32Snapshot", (dword, dword), handle),
                ("Thread32First", (handle, ctypes.c_void_p), bool_),
                ("Thread32Next", (handle, ctypes.c_void_p), bool_),
                ("CloseHandle", (handle,), bool_),
            ):
                function = getattr(kernel, name)
                function.argtypes, function.restype = args, result
            ntdll = ctypes.WinDLL("ntdll")
            resume = getattr(ntdll, "NtResumeProcess", None)
            if resume is not None:
                resume.argtypes, resume.restype = (handle,), ctypes.c_long

            class BasicLimits(ctypes.Structure):
                _fields_ = [
                    ("PerProcessUserTimeLimit", ctypes.c_longlong),
                    ("PerJobUserTimeLimit", ctypes.c_longlong),
                    ("LimitFlags", dword),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", dword),
                    ("Affinity", ctypes.c_size_t),
                    ("PriorityClass", dword),
                    ("SchedulingClass", dword),
                ]

            class IoCounters(ctypes.Structure):
                _fields_ = [(n, ctypes.c_ulonglong) for n in (
                    "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                    "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

            class ExtendedLimits(ctypes.Structure):
                _fields_ = [
                    ("BasicLimitInformation", BasicLimits),
                    ("IoInfo", IoCounters),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t),
                ]

            class ThreadEntry(ctypes.Structure):
                _fields_ = [
                    ("dwSize", dword), ("cntUsage", dword), ("th32ThreadID", dword),
                    ("th32OwnerProcessID", dword), ("tpBasePri", wintypes.LONG),
                    ("tpDeltaPri", wintypes.LONG), ("dwFlags", dword),
                ]

            kernel.ctypes, kernel.wintypes = ctypes, wintypes
            kernel.ExtendedLimits, kernel.ThreadEntry = ExtendedLimits, ThreadEntry
            kernel.nt_resume = resume
            cls._api = kernel
        return cls._api

    @classmethod
    def handle(cls):
        """The job's handle, or None where Windows refused one (clients then
        run outside a job, as before)."""
        if not _WINDOWS or cls._failed:
            return None
        if cls._handle is None:
            with cls._lock:
                if cls._handle is None and not cls._failed:
                    try:
                        api = cls.api()
                        job = api.CreateJobObjectW(None, None)
                        info = api.ExtendedLimits()
                        info.BasicLimitInformation.LimitFlags = _KILL_ON_JOB_CLOSE | _BREAKAWAY
                        if job and api.SetInformationJobObject(
                            job, 9, api.ctypes.byref(info), api.ctypes.sizeof(info)  # extended limits
                        ):
                            cls._handle = job
                        else:
                            if job:
                                api.CloseHandle(job)
                            cls._failed = True
                    except Exception:
                        cls._failed = True
        return cls._handle

    @classmethod
    def admit(cls, proc) -> bool:
        """Put the suspended *proc* into the job, then let it run.  Never leaves
        it suspended: when it can't be resumed it is killed and OSError raised."""
        api = cls.api()
        process = int(getattr(proc, "_handle", 0) or 0)
        opened = None
        if not process:
            # PROCESS_SET_QUOTA | PROCESS_TERMINATE | PROCESS_SUSPEND_RESUME
            opened = api.OpenProcess(0x0100 | 0x0001 | 0x0800, False, proc.pid)
            process = int(opened or 0)
        try:
            job = cls.handle()
            admitted = bool(job and process and api.AssignProcessToJobObject(job, process))
            resumed = cls._resume(api, process, proc.pid)
        finally:
            if opened:
                api.CloseHandle(opened)
        if not resumed:
            try:
                proc.kill()
            except OSError:
                pass
            raise OSError("adb could not be started: Windows did not let it run")
        return admitted

    @staticmethod
    def _resume(api, process, pid) -> bool:
        # One call on the process handle; a snapshot of every thread on the
        # PC (a tenth of a second with thousands of threads) only without it.
        if api.nt_resume is not None and process:
            try:
                if api.nt_resume(process) == 0:
                    return True
            except Exception:
                pass
        snapshot = api.CreateToolhelp32Snapshot(0x00000004, 0)  # TH32CS_SNAPTHREAD
        if not snapshot or snapshot == api.ctypes.c_void_p(-1).value:
            return False
        resumed = 0
        try:
            entry = api.ThreadEntry()
            entry.dwSize = api.ctypes.sizeof(entry)
            more = api.Thread32First(snapshot, api.ctypes.byref(entry))
            while more:
                if entry.th32OwnerProcessID == pid:
                    thread = api.OpenThread(0x0002, False, entry.th32ThreadID)  # SUSPEND_RESUME
                    if thread:
                        if api.ResumeThread(thread) != 0xFFFFFFFF:
                            resumed += 1
                        api.CloseHandle(thread)
                more = api.Thread32Next(snapshot, api.ctypes.byref(entry))
        finally:
            api.CloseHandle(snapshot)
        return resumed > 0


def in_job(proc_or_pid) -> Optional[bool]:
    """Whether a process (a Popen or a PID) is in TurboADB's adb job; None
    where there is no such job (not Windows, or Windows refused one)."""
    job = _Job.handle()
    if job is None:
        return None
    api = _Job.api()
    pid = getattr(proc_or_pid, "pid", proc_or_pid)
    process = api.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
    if not process:
        return False
    try:
        result = api.wintypes.BOOL()
        if not api.IsProcessInJob(process, job, api.ctypes.byref(result)):
            return False
        return bool(result.value)
    finally:
        api.CloseHandle(process)


# --------------------------------------------------------------------------- #
# Starting and waiting
# --------------------------------------------------------------------------- #
_REAL_POPEN = subprocess.Popen


def popen(cmd, **kwargs) -> subprocess.Popen:
    """``subprocess.Popen(cmd, **kwargs)`` for an adb client: on Windows in
    this process's kill-on-close job (see the module docstring).  Raises
    OSError as Popen does.  A stand-in for Popen (a test's) is called as it
    is: only a real process can be put into a job."""
    if subprocess.Popen is not _REAL_POPEN or _Job.handle() is None:
        return subprocess.Popen(cmd, **kwargs)
    kwargs["creationflags"] = int(kwargs.get("creationflags", 0) or 0) | _CREATE_SUSPENDED
    proc = subprocess.Popen(cmd, **kwargs)
    _Job.admit(proc)
    return proc


class Finished(NamedTuple):
    """How a client run to its end by :func:`run` ended."""

    returncode: int
    stdout: bytes
    stderr: bytes
    ended: str  # "" by itself; "timeout" or "cancelled": TurboADB ended it


def end(proc) -> tuple:
    """Kill *proc* (only it) and read what it printed, for :data:`_DRAIN_S`
    at most; returns ``(stdout, stderr)`` bytes."""
    try:
        proc.kill()
    except OSError:
        pass
    try:
        out, err = proc.communicate(timeout=_DRAIN_S)
    except (subprocess.TimeoutExpired, ValueError, OSError):
        out = err = b""  # something it started holds the pipes: let it be
    return out or b"", err or b""


def run(cmd, *, timeout=None, cancel=None, input=None, creationflags=NO_WINDOW,
        poll=POLL_S, **kwargs) -> Finished:
    """Run the adb client *cmd* to its end.  *timeout* (seconds, None: no
    limit) and *cancel* (see :func:`cancel_check`) end it at once, killing
    only it: ``ended`` then says which, and ``returncode`` is
    :data:`TIMEOUT_EXIT_CODE`.  *input* (bytes) is written to its stdin.
    Raises OSError when it can't be started."""
    stop = cancel_check(cancel)
    if stop is not None and stop():
        return Finished(TIMEOUT_EXIT_CODE, b"", b"", "cancelled")
    proc = popen(
        cmd,
        stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=creationflags,
        **kwargs,
    )
    deadline = None if timeout is None else time.monotonic() + max(0.0, float(timeout))
    pending = input
    ended = ""
    try:
        while True:
            step = None if stop is None else poll
            if deadline is not None:
                left = max(0.0, deadline - time.monotonic())
                step = left if step is None else min(step, left)
            try:
                out, err = proc.communicate(input=pending, timeout=step)
                return Finished(proc.returncode, out or b"", err or b"", "")
            except subprocess.TimeoutExpired:
                pending = None  # (written already: only the first call may)
            if stop is not None and stop():
                ended = "cancelled"
                break
            if deadline is not None and time.monotonic() >= deadline:
                ended = "timeout"
                break
    except BaseException:
        end(proc)  # Ctrl+C, an error of ours: never leave the client running
        raise
    out, err = end(proc)
    return Finished(TIMEOUT_EXIT_CODE, out, err, ended)


_REAL_RUN = subprocess.run


def run_compat(cmd, *, input=None, capture_output=False, timeout=None, check=False, **kwargs):
    """:func:`subprocess.run` for an adb client, started by :func:`popen` (in
    the job): the same arguments and result, ``TimeoutExpired`` included.  A
    stand-in for subprocess.run (a test's) is called as it is."""
    if subprocess.run is not _REAL_RUN:
        return subprocess.run(cmd, input=input, capture_output=capture_output, timeout=timeout,
                              check=check, **kwargs)
    if input is not None:
        kwargs["stdin"] = subprocess.PIPE
    if capture_output:
        kwargs["stdout"] = subprocess.PIPE
        kwargs["stderr"] = subprocess.PIPE
    with popen(cmd, **kwargs) as proc:
        try:
            out, err = proc.communicate(input, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            exc.stdout, exc.stderr = end(proc)
            raise
        except BaseException:
            end(proc)
            raise
        code = proc.poll()
    if check and code:
        raise subprocess.CalledProcessError(code, proc.args, output=out, stderr=err)
    return subprocess.CompletedProcess(proc.args, code, out, err)


_REAL_CALL = subprocess.call


def call(cmd, *, timeout=None, **kwargs) -> int:
    """:func:`subprocess.call` for an adb client that shares this console
    (``turboadb shell``), started by :func:`popen` (in the job): its exit
    code; ``TimeoutExpired`` after *timeout*, the client ended.  A stand-in
    for subprocess.call (a test's) is called as it is."""
    if subprocess.call is not _REAL_CALL:
        return subprocess.call(cmd, timeout=timeout, **kwargs)
    with popen(cmd, **kwargs) as proc:
        try:
            return proc.wait(timeout=timeout)
        except BaseException:  # the timeout, Ctrl+C: the client never outlives the call
            proc.kill()
            raise


def run_text(cmd, *, timeout=None, **kwargs) -> subprocess.CompletedProcess:
    """:func:`run_compat` with the output captured and read as text by
    :func:`decode` (stdout and stderr are ``str``, never None).  Raises
    ``TimeoutExpired`` (its output bytes) and OSError as subprocess.run does."""
    kwargs.pop("text", None)
    kwargs.pop("encoding", None)
    kwargs.pop("errors", None)
    done = run_compat(cmd, capture_output=True, timeout=timeout, **kwargs)
    return subprocess.CompletedProcess(getattr(done, "args", cmd), done.returncode,
                                       decode(done.stdout), decode(done.stderr))


# --------------------------------------------------------------------------- #
# Reading adb's output
# --------------------------------------------------------------------------- #
def _oem_codec() -> Optional[str]:
    if not _WINDOWS:
        return None
    try:
        import codecs

        codecs.lookup("oem")
        return "oem"
    except LookupError:
        return None


_OEM = _oem_codec()


def decode_line(line: bytes) -> str:
    """One line of adb's output: UTF-8 when it is valid UTF-8, else the OEM
    code page (else UTF-8 with replacement characters).  Never fails."""
    try:
        return line.decode("utf-8")
    except UnicodeDecodeError:
        pass
    if _OEM is not None:
        try:
            return line.decode(_OEM)
        except UnicodeDecodeError:
            pass
    return line.decode("utf-8", errors="replace")


def decode(data, encoding: str = "utf-8") -> str:
    """adb's output as text, line by line (see :func:`decode_line`); never
    fails and never None.  Another *encoding* than UTF-8 (set on purpose:
    ``ADBConfig.encoding``) is used as it is, with replacement characters."""
    if data is None:
        return ""
    if isinstance(data, str):
        return data
    if (encoding or "utf-8").replace("_", "-").lower() not in ("utf-8", "utf8"):
        return data.decode(encoding, errors="replace")
    try:
        return data.decode("utf-8")  # (all of it at once: the usual case)
    except UnicodeDecodeError:
        return "".join(decode_line(line) for line in data.splitlines(keepends=True))
