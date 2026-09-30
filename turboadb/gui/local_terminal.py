"""Interactive local terminal session for PowerShell and CMD inside TurboADB.

Binds ANDROID_SERIAL and TurboADB's verified platform-tools path to the subprocess
environment so the user can immediately run adb commands (e.g. adb shell logcat -d)
or arbitrary local scripts directly in the terminal tab.

The shell must behave like a fresh Windows Terminal / cmd window, so its
environment is built by the pure :func:`build_shell_env`:

* TurboADB's private process state never reaches the shell.  A frozen
  (PyInstaller) build sets ``_PYI_*`` bootloader variables, ``QT_PLUGIN_PATH`` /
  ``QML2_IMPORT_PATH`` and puts its ``_MEI…`` extraction folder (with bundled
  ``ucrtbase``/``VCRUNTIME140``/OpenSSL/Qt DLLs) on ``PATH``; PyQt5 prepends its
  ``Qt5\\bin`` in a source run.  The bootloader's ``SetDllDirectory`` is also
  inherited by every child process (:func:`_popen_clean_dll_path`).
* Variables added in the registry after TurboADB started are picked up, with
  ``REG_EXPAND_SZ`` values expanded the way Windows does (``%NAME%`` only) and
  the user ``Path`` appended after the machine ``Path``.
* ``NoDefaultCurrentDirectoryInExePath`` inherited from a launcher shell (some
  some IDE terminals set it) is dropped unless the registry sets it, so
  ``tool.exe`` in the current folder runs in cmd as in a normal window.

Redirected pipes use the console code page, not UTF-8: PowerShell is switched to
UTF-8 input/output at startup, cmd input is encoded in the OEM code page, and
output is normalised to UTF-8 by :class:`OutputTranscoder`.

With no console, the shells need a few stand-ins for what a console window
gives them: Python tools run unbuffered, input lines end in LF (a CR left
behind made ``pause`` show the prompt twice), PowerShell's ``Read-Host`` is
replaced by one that shows its prompt, and every prompt ends with an invisible
mark (:data:`PROMPT_MARK_RE`) that tells the terminal the shell waits for a
command, and in which folder.  Input is written by a thread of its own, so a
shell that does not read it never blocks the window.  Stop presses Ctrl+C in
the shell's hidden console (:meth:`LocalShellSession.send_ctrl_c`), and ends
the command's processes only when it ignores that
(:meth:`LocalShellSession.kill_command`); the shell stays.
"""

from __future__ import annotations

import codecs
import ntpath
import os
import posixpath
import re
import subprocess
import sys
import threading
import time
from collections import deque
from typing import Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Union

from .. import proctree
from ..config import adb_server_address
from ..tools import DEFAULT_ADB_SERVER_PORT, find_adb, NO_WINDOW

# Hoisted out of the per-poll read path: ShellSession.read() runs many times
# a second per open terminal, and re-importing these each time is wasted work.
if os.name == "nt":
    import ctypes as _ctypes
    import msvcrt as _msvcrt
    from ctypes import wintypes as _wintypes

    _PEEK_NAMED_PIPE = _ctypes.windll.kernel32.PeekNamedPipe
else:  # pragma: no cover - POSIX has no named-pipe peek
    _ctypes = _msvcrt = _wintypes = None
    _PEEK_NAMED_PIPE = None


# (name, raw value, registry type) as returned by winreg.EnumValue.
RegValue = Tuple[str, str, int]

_REG_SZ = 1  # winreg.REG_SZ
_REG_EXPAND_SZ = 2  # winreg.REG_EXPAND_SZ
_SYSTEM_ENV_KEY = r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"

# PyInstaller bootloader state: a child that inherits it believes it is part of
# TurboADB's own process tree (another frozen app reuses the wrong bundle).
_FROZEN_PREFIXES = ("_PYI_", "_MEIPASS")
_NO_CWD_EXE = "NoDefaultCurrentDirectoryInExePath"
_DEFAULT_PATHEXT = ".COM;.EXE;.BAT;.CMD;.VBS;.VBE;.JS;.JSE;.WSF;.WSH;.MSC"
_PERCENT_VAR = re.compile(r"%([^%\r\n]+)%")

# Windows PowerShell decodes redirected stdin and encodes its output with the
# OEM code page, so ``cd café`` reached it as ``cafÃ©`` and failed.  Switch the
# session to UTF-8 both ways; -NoExit keeps it interactive and $PROFILE still
# loads first.  try/catch keeps a ConstrainedLanguage session usable.
_PS_UTF8_INIT = (
    "try{$__tadbUtf8=New-Object System.Text.UTF8Encoding $false;"
    "[Console]::InputEncoding=$__tadbUtf8;[Console]::OutputEncoding=$__tadbUtf8;"
    "$global:OutputEncoding=$__tadbUtf8}catch{};"
    "Remove-Variable __tadbUtf8 -ErrorAction SilentlyContinue"
)

# Every prompt ends with this mark: an OSC string, which the console drops like
# any other, carrying the shell's folder.  The terminal knows from it that the
# shell waits for a command, whatever the prompt looks like (oh-my-posh, a
# PROMPT variable).  cmd writes it through PROMPT ($E is ESC, $P the folder).
PROMPT_MARK_RE = re.compile(r"\x1b\]7717;([^\x07\x1b]*)(?:\x07|\x1b\\)")
_CMD_PROMPT_MARK = "$E]7717;$P$E\\"

# The rest of the PowerShell init needs FullLanguage (method calls on .NET
# types), so a ConstrainedLanguage session just keeps PowerShell's own.
# * Read-Host: PowerShell's own never shows its prompt without a console and
#   echoes the answer; this one writes the prompt, reads the line unechoed and
#   still returns a SecureString for -AsSecureString.
# * The prompt function is wrapped to end with the mark.
_PS_READ_HOST = (
    "function global:Read-Host{[CmdletBinding()]param("
    "[Parameter(Position=0,ValueFromRemainingArguments=$true)]$Prompt,[switch]$AsSecureString)"
    "if($null -ne $Prompt){[Console]::Out.Write(($Prompt -join ' ')+': ')};"
    "[Console]::Out.Flush();$__line=[Console]::In.ReadLine();"
    "if($AsSecureString){if($null -eq $__line){$__line=''};"
    "ConvertTo-SecureString -String $__line -AsPlainText -Force}else{$__line}}"
)
_PS_PROMPT_MARK = (
    "$global:__tadbPrompt=$function:prompt;"
    "function global:prompt{$__p=& $global:__tadbPrompt;"
    "([string]$__p)+[char]27+']7717;'+$PWD.ProviderPath+[char]7}"
)
# Width of the hidden console's buffer: PowerShell wraps formatted output
# (Select-String matches, tables) at it, 120 columns by default.
_PS_MIN_WIDTH = 120
_PS_MAX_WIDTH = 1000


def ps_buffer_width(columns: Optional[int]) -> int:
    """The buffer width PowerShell formats to in a view *columns* wide: never
    narrower than a console's own 120 columns, at most 1000."""
    if columns and columns > _PS_MIN_WIDTH:
        return min(int(columns), _PS_MAX_WIDTH)
    return _PS_MIN_WIDTH


def ps_resize_command(width: int) -> str:
    """A PowerShell statement (ending in ``;``) that gives the buffer *width*
    columns, for the terminal to put in front of the next command typed at
    PowerShell's prompt once its view was resized."""
    return (
        "if($ExecutionContext.SessionState.LanguageMode -eq 'FullLanguage'){"
        f"try{{$__tadbSize=$Host.UI.RawUI.BufferSize;$__tadbSize.Width={int(width)};"
        "$Host.UI.RawUI.BufferSize=$__tadbSize}catch{};"
        "Remove-Variable __tadbSize -ErrorAction SilentlyContinue};"
    )


def _ps_init(columns: Optional[int] = None) -> str:
    """The script PowerShell runs at startup (after the user's profile)."""
    full = []
    if columns and columns > _PS_MIN_WIDTH:
        width = min(int(columns), _PS_MAX_WIDTH)
        full.append(
            f"try{{$__tadbSize=$Host.UI.RawUI.BufferSize;if($__tadbSize.Width -lt {width})"
            f"{{$__tadbSize.Width={width};$Host.UI.RawUI.BufferSize=$__tadbSize}}}}catch{{}};"
            "Remove-Variable __tadbSize -ErrorAction SilentlyContinue;"
        )
    full.append("try{" + _PS_READ_HOST + "}catch{};try{" + _PS_PROMPT_MARK + "}catch{}")
    return (
        _PS_UTF8_INIT
        + ";if($ExecutionContext.SessionState.LanguageMode -eq 'FullLanguage'){"
        + "".join(full) + "}"
    )


def _expand(value: str, lookup_upper: Mapping[str, str]) -> str:
    """``ExpandEnvironmentStrings`` semantics: ``%NAME%`` only, case-insensitive,
    an undefined name stays literal (``os.path.expandvars`` also rewrote ``$x``)."""
    return _PERCENT_VAR.sub(lambda m: lookup_upper.get(m.group(1).upper(), m.group(0)), value)


def expand_registry_env(
    system: Sequence[RegValue],
    user: Sequence[RegValue],
    volatile: Sequence[RegValue] = (),
    base_env: Optional[Mapping[str, str]] = None,
) -> Dict[str, str]:
    """The variables a new logon session gets from the registry.

    Mirrors Windows: volatile session values, machine values, then user values;
    within a scope ``REG_SZ`` before ``REG_EXPAND_SZ`` (expanded against
    everything known so far, *base_env* first); the user ``Path`` is appended to
    the machine ``Path``.  Names keep the registry's spelling.
    """
    lookup: Dict[str, str] = {}
    for name, value, _kind in volatile:
        if isinstance(value, str):
            lookup[name.upper()] = value
    for name, value in (base_env or {}).items():
        lookup[name.upper()] = value

    spelled: Dict[str, str] = {}
    result: Dict[str, str] = {}
    for scope, values in (("volatile", volatile), ("system", system), ("user", user)):
        ordered = [v for v in values if v[2] == _REG_SZ] + [v for v in values if v[2] == _REG_EXPAND_SZ]
        for name, raw, kind in ordered:
            if not isinstance(raw, str):
                continue
            key = name.upper()
            value = _expand(raw, lookup) if kind == _REG_EXPAND_SZ else raw
            if key == "PATH" and scope == "user" and result.get(key):
                value = result[key].rstrip(";") + (";" + value if value else "")
            spelled.setdefault(key, name)
            result[key] = value
            if scope != "volatile":
                lookup[key] = value
    return {spelled[key]: value for key, value in result.items()}


def _read_registry_values(root, subkey: str) -> List[RegValue]:
    import winreg

    values: List[RegValue] = []
    try:
        with winreg.OpenKey(root, subkey) as key:
            index = 0
            while True:
                try:
                    name, value, kind = winreg.EnumValue(key, index)
                except OSError:
                    break
                index += 1
                if isinstance(value, str) and kind in (_REG_SZ, _REG_EXPAND_SZ):
                    values.append((name, value, kind))
    except OSError:
        pass
    return values


def _get_registry_env() -> Dict[str, str]:
    """Current machine, user and volatile environment from the registry."""
    if os.name != "nt":
        return {}
    try:
        import winreg
    except ImportError:
        return {}
    return expand_registry_env(
        _read_registry_values(winreg.HKEY_LOCAL_MACHINE, _SYSTEM_ENV_KEY),
        _read_registry_values(winreg.HKEY_CURRENT_USER, "Environment"),
        _read_registry_values(winreg.HKEY_CURRENT_USER, "Volatile Environment"),
        os.environ,
    )


def is_pwsh_module_path(entry: str, isfile=os.path.isfile) -> bool:
    """Whether the ``PSModulePath`` *entry* is PowerShell 7's (pwsh's) rather
    than Windows PowerShell's: ``Documents\\PowerShell\\Modules``,
    ``Program Files\\PowerShell\\Modules``, or the ``Modules`` folder of a
    pwsh installation (beside ``pwsh.dll``).  The rule pwsh applies when it
    starts powershell.exe (``GetWindowsPowerShellModulePath``)."""
    path = ntpath.normpath(entry.strip().strip('"')).rstrip("\\")
    parts = path.lower().split("\\")
    if len(parts) >= 2 and parts[-2:] == ["powershell", "modules"]:
        return True
    parent = ntpath.dirname(path)
    return bool(parent) and parent != path and isfile(ntpath.join(parent, "pwsh.dll"))


def build_shell_env(
    base_env: Mapping[str, str],
    *,
    registry_env: Optional[Mapping[str, str]] = None,
    private_roots: Iterable[str] = (),
    prepend_dirs: Iterable[Optional[str]] = (),
    append_dirs: Iterable[Optional[str]] = (),
    serial: Optional[str] = None,
    adb_exe: Optional[str] = None,
    system_root: Optional[str] = None,
    windows: bool = True,
    adb_server_host: Optional[str] = None,
    adb_server_port: Optional[int] = None,
    mark_prompt: bool = False,
) -> Dict[str, str]:
    """Environment block for a local shell (pure; no process or registry access).

    * *base_env* (TurboADB's ``os.environ``) minus PyInstaller ``_PYI_*`` state,
      and minus every value / ``PATH`` entry inside *private_roots* (the
      ``_MEI…`` bundle, PyQt5's package folder).  Names are case-insensitive.
    * Registry variables TurboADB does not have yet are added; inherited values
      win, so a launcher's deliberate overrides survive.
    * ``PATH`` = *prepend_dirs* (ADB), the inherited ``PATH``, new registry
      entries, the Windows system folders if missing, then *append_dirs*
      (scrcpy); de-duplicated ignoring case, quotes and trailing slashes.
    * ``PSModulePath`` without PowerShell 7's own folders
      (:func:`is_pwsh_module_path`), as pwsh itself starts powershell.exe.
      Inherited from a pwsh terminal they made Windows PowerShell load pwsh's
      PSReadLine, which fails there ("Cannot load PSReadline module"), and
      the first command typed was lost.
    * ``PYTHONUNBUFFERED=1`` unless it is set: over a pipe Python holds its
      output until it exits, where a console window shows every line.
    * The device tab's adb server (*adb_server_host* / *adb_server_port*), so
      a typed ``adb`` reaches the same server as TurboADB's own commands
      (``ADBHandler._base`` passes ``-H``/``-P``); ``ADB_SERVER_SOCKET``, which
      adb would prefer, is dropped then.  adb wraps
      ``ANDROID_ADB_SERVER_ADDRESS`` into ``tcp:<host>:<port>`` itself, so it
      gets the bare host, an IPv6 literal in brackets as for ``-H`` (see
      ``scrcpy._server_env``).
    * *mark_prompt*: cmd's ``PROMPT`` ends with the prompt mark
      (:data:`PROMPT_MARK_RE`).
    """
    sep = ";" if windows else ":"
    pathmod = ntpath if windows else posixpath

    def unquote(entry: str) -> str:
        entry = entry.strip()
        if len(entry) >= 2 and entry[0] == entry[-1] == '"':
            entry = entry[1:-1].strip()
        return entry

    def norm(entry: str) -> str:
        entry = unquote(entry)
        return pathmod.normcase(pathmod.normpath(entry)) if entry else ""

    roots = [root.rstrip("\\/") for root in (norm(p) for p in private_roots if p) if root]

    def private(entry: str) -> bool:
        key = norm(entry)
        return bool(key) and any(
            key == root or key.startswith(root + "\\") or key.startswith(root + "/")
            for root in roots
        )

    env: Dict[str, str] = {}
    keys: Dict[str, str] = {}  # UPPER -> spelling used in env

    def get(name: str) -> Optional[str]:
        key = keys.get(name.upper())
        return None if key is None else env[key]

    def put(name: str, value: str) -> None:
        env[keys.setdefault(name.upper(), name)] = value

    def drop(name: str) -> None:
        key = keys.pop(name.upper(), None)
        if key is not None:
            del env[key]

    for name, value in base_env.items():
        upper = name.upper()
        if upper in keys or upper.startswith(_FROZEN_PREFIXES):
            continue
        if roots and upper != "PATH":
            parts = value.split(sep)
            kept = [part for part in parts if not private(part)]
            if len(kept) != len(parts):
                if not any(part.strip() for part in kept):
                    continue
                value = sep.join(kept)
        put(name, value)

    registry = {name.upper(): (name, value) for name, value in (registry_env or {}).items()}
    for upper, (name, value) in registry.items():
        if upper != "PATH" and get(name) is None:
            put(name, value)
    if _NO_CWD_EXE.upper() not in registry and _NO_CWD_EXE.upper() in keys:
        del env[keys.pop(_NO_CWD_EXE.upper())]

    entries: List[str] = []
    seen = set()

    def add(entry: str, *, allow_private: bool = False) -> None:
        clean = unquote(entry)
        key = norm(clean)
        if not key or key in seen or (not allow_private and private(clean)):
            return
        seen.add(key)
        entries.append(clean)

    for directory in prepend_dirs:
        if directory:
            add(directory, allow_private=True)
    for entry in (get("PATH") or "").split(sep):
        add(entry)
    for entry in registry.get("PATH", ("", ""))[1].split(sep):
        add(entry)
    root = system_root or get("SystemRoot") or get("windir") or r"C:\Windows"
    if windows:
        for entry in (
            ntpath.join(root, "System32"),
            root,
            ntpath.join(root, "System32", "Wbem"),
            ntpath.join(root, "System32", "WindowsPowerShell", "v1.0"),
        ):
            add(entry)
    for directory in append_dirs:
        if directory:
            add(directory, allow_private=True)
    put("PATH", sep.join(entries))

    modules = get("PSModulePath")
    if windows and modules:
        kept = [entry for entry in modules.split(";") if entry.strip() and not is_pwsh_module_path(entry)]
        if kept:
            put("PSModulePath", ";".join(kept))
        else:
            drop("PSModulePath")  # Windows PowerShell builds its own

    if windows:
        # Without SystemRoot, Winsock (ipconfig, ping, adb) and .NET fail to
        # initialise; restore the essentials a stripped launcher may lack.
        for name, value in (
            ("SystemRoot", root),
            ("windir", root),
            ("SystemDrive", root[:2]),
            ("ComSpec", ntpath.join(root, "System32", "cmd.exe")),
            ("PATHEXT", _DEFAULT_PATHEXT),
        ):
            if not get(name):
                put(name, value)
    # Python writes pipes in the ANSI code page and crashes on e.g. "✓"; a real
    # console would print it.  Respect any explicit user choice.
    if get("PYTHONIOENCODING") is None and get("PYTHONUTF8") is None:
        put("PYTHONIOENCODING", "utf-8")
    if get("PYTHONUNBUFFERED") is None:
        put("PYTHONUNBUFFERED", "1")
    if windows and mark_prompt:
        prompt = get("PROMPT") or "$P$G"
        if not prompt.endswith(_CMD_PROMPT_MARK):
            put("PROMPT", prompt + _CMD_PROMPT_MARK)
    if serial:
        put("ANDROID_SERIAL", serial)
    if adb_exe:
        put("ADB", adb_exe)
    if adb_server_host:
        drop("ADB_SERVER_SOCKET")
        put("ANDROID_ADB_SERVER_ADDRESS", adb_server_address(adb_server_host))
        put("ANDROID_ADB_SERVER_PORT", str(adb_server_port or DEFAULT_ADB_SERVER_PORT))
    elif adb_server_port and int(adb_server_port) != DEFAULT_ADB_SERVER_PORT:
        drop("ADB_SERVER_SOCKET")
        put("ANDROID_ADB_SERVER_PORT", str(adb_server_port))
    return env


def _private_roots() -> List[str]:
    """Folders that belong to TurboADB's own runtime, never to a user shell."""
    roots: List[str] = []
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle:
        roots.append(bundle)
    qt_file = getattr(sys.modules.get("PyQt5"), "__file__", None)
    if qt_file:
        roots.append(os.path.dirname(os.path.abspath(qt_file)))
    return roots


def _system_root() -> Optional[str]:
    root = os.environ.get("SystemRoot") or os.environ.get("windir")
    if root or os.name != "nt":
        return root
    try:
        import ctypes

        buf = ctypes.create_unicode_buffer(260)
        if ctypes.windll.kernel32.GetSystemWindowsDirectoryW(buf, 260):
            return buf.value
    except Exception:
        pass
    return r"C:\Windows"


def _console_codec() -> str:
    """Codec of the console (OEM) code page used by cmd and console tools."""
    if os.name == "nt":
        try:
            codecs.lookup("oem")
            return "oem"
        except LookupError:
            pass
    return "utf-8"


def shell_argv(shell_type: str, system_root: Optional[str] = None,
               columns: Optional[int] = None) -> List[str]:
    """Absolute shell path, so a ``cmd.exe`` beside TurboADB can't be picked.

    *columns*: the terminal's width, which PowerShell formats its output to.
    PowerShell keeps the execution policy of this PC, as in a PowerShell
    window: the startup script is passed with ``-Command``, which no policy
    blocks, so ``-ExecutionPolicy Bypass`` would only have let scripts run
    here that the policy stops everywhere else."""
    root = system_root or r"C:\Windows"
    if shell_type == "powershell":
        exe = ntpath.join(root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
        return [
            exe if os.path.isfile(exe) else "powershell.exe",
            "-NoLogo", "-NoExit", "-Command", _ps_init(columns),
        ]
    exe = ntpath.join(root, "System32", "cmd.exe")
    return [exe if os.path.isfile(exe) else "cmd.exe"]


def _system_exe(name: str) -> str:
    """*name* in System32 by absolute path (like the shells themselves), else
    the bare name.  A same-named exe beside TurboADB is never picked."""
    exe = ntpath.join(_system_root() or r"C:\Windows", "System32", name)
    return exe if os.path.isfile(exe) else name


def gnu_grep(env: Mapping[str, str]) -> bool:
    """Whether the ``grep`` a shell with *env* runs is GNU grep (Git for
    Windows, MSYS2, Cygwin), which takes ``--line-buffered``.  Runs ``grep
    --version``: call it off the UI thread."""
    import shutil

    path = next((value for key, value in env.items() if key.upper() == "PATH"), "")
    exe = shutil.which("grep", path=path) if path else None
    if not exe:
        return False
    try:
        out = subprocess.run(
            [exe, "--version"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env=dict(env), creationflags=NO_WINDOW, timeout=5,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return b"GNU grep" in (out or b"")


# Held while a shell is started and while this process is on a shell's console
# to raise its Ctrl+C (see _console_ctrl_c).  It is the process's one console
# lock, which scrcpy's Ctrl+Break takes too: this process can be on only one
# other console at a time.
_SPAWN_LOCK = proctree.CONSOLE_LOCK
# This process ignores Ctrl+C: set once it raised one on a shell's console
# from inside (the event reaches every process on that console, this one too).
_ignoring_ctrl_c = False
_CTRL_C_EVENT = 0
_ERROR_ACCESS_DENIED = 5
# Raises Ctrl+C on a shell's console from a short-lived helper, for a TurboADB
# that has a console of its own (run from a terminal: AttachConsole refuses a
# second one).  The helper ignores the Ctrl+C itself.
_CTRL_C_HELPER = (
    "import ctypes,sys\n"
    "k=ctypes.windll.kernel32\n"
    "k.FreeConsole()\n"
    "ok=k.AttachConsole(int(sys.argv[1])) and k.SetConsoleCtrlHandler(None,1)"
    " and k.GenerateConsoleCtrlEvent(0,0)\n"
    "sys.exit(0 if ok else 1)\n"
)


def _kernel32():
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.SetConsoleCtrlHandler.argtypes = (ctypes.c_void_p, wintypes.BOOL)
    kernel32.SetConsoleCtrlHandler.restype = wintypes.BOOL
    kernel32.AttachConsole.argtypes = (wintypes.DWORD,)
    kernel32.AttachConsole.restype = wintypes.BOOL
    kernel32.FreeConsole.restype = wintypes.BOOL
    kernel32.GenerateConsoleCtrlEvent.argtypes = (wintypes.DWORD, wintypes.DWORD)
    kernel32.GenerateConsoleCtrlEvent.restype = wintypes.BOOL
    kernel32.GetStdHandle.argtypes = (wintypes.DWORD,)
    kernel32.GetStdHandle.restype = wintypes.HANDLE
    kernel32.SetStdHandle.argtypes = (wintypes.DWORD, wintypes.HANDLE)
    kernel32.SetStdHandle.restype = wintypes.BOOL
    return kernel32


# DLL folders kept for this process with AddDllDirectory (see _keep_dll_directory).
_KEPT_DLL_DIRS = {}


def _keep_dll_directory(path: str) -> None:
    """Keep *path* on this process's DLL search path with AddDllDirectory,
    once.  Python loads extension modules (and so their DLLs) searching those
    folders too, so another thread that imports one while
    :func:`_popen_clean_dll_path` has the ``SetDllDirectory`` path cleared
    still finds TurboADB's bundled DLLs.  Unlike that path, it is not handed
    to child processes."""
    if path in _KEPT_DLL_DIRS:
        return
    add = getattr(os, "add_dll_directory", None)  # Windows, Python 3.8+
    try:
        _KEPT_DLL_DIRS[path] = add(path) if add is not None else None
    except OSError:
        _KEPT_DLL_DIRS[path] = None


def _popen_clean_dll_path(argv, **kwargs) -> subprocess.Popen:
    """``subprocess.Popen`` without passing on a ``SetDllDirectory`` path, and
    with Ctrl+C handled normally in the child.

    PyInstaller's bootloader points the DLL search path at its ``_MEI…`` folder
    and Windows hands that to every child, which then loads TurboADB's bundled
    ucrtbase/VCRUNTIME140/libssl instead of its own.  The path is cleared only
    for the duration of CreateProcess and restored for TurboADB itself; the
    folder stays searchable meanwhile for other threads' imports
    (:func:`_keep_dll_directory`).  A child also inherits "ignore Ctrl+C"
    (from TurboADB's launcher, or set by :func:`_console_ctrl_c`), which would
    make Stop's Ctrl+C miss the shell.
    """
    if os.name != "nt":
        return subprocess.Popen(argv, **kwargs)
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = _kernel32()
        get_dir = kernel32.GetDllDirectoryW
        get_dir.argtypes = (wintypes.DWORD, wintypes.LPWSTR)
        get_dir.restype = wintypes.DWORD
        set_dir = kernel32.SetDllDirectoryW
        set_dir.argtypes = (wintypes.LPCWSTR,)
        set_dir.restype = wintypes.BOOL
        buf = ctypes.create_unicode_buffer(32768)
        saved = buf.value if get_dir(len(buf), buf) else ""
    except Exception:
        return subprocess.Popen(argv, **kwargs)
    with _SPAWN_LOCK:
        if saved:
            _keep_dll_directory(saved)
            set_dir(None)
        kernel32.SetConsoleCtrlHandler(None, False)
        try:
            return subprocess.Popen(argv, **kwargs)
        finally:
            if _ignoring_ctrl_c:
                kernel32.SetConsoleCtrlHandler(None, True)
            if saved:
                set_dir(saved)


def _console_ctrl_c(pid: int) -> bool:
    """Raise Ctrl+C on the console of the shell *pid*, as pressing it in a
    console window does: every process on that console gets it (the shell,
    the command it runs, a pipeline's programs, a background job), never an
    adb server (it has no console).  True when the event was raised.  Blocks
    for a moment: call it off the UI thread.

    The shells run on hidden consoles of their own (CREATE_NO_WINDOW).
    Without a console of its own, TurboADB attaches to the shell's console
    for the call, ignoring the Ctrl+C itself from then on; with one (a run
    from a terminal) a helper process does it, when there is a Python to run
    it (not in the one-file exe, which never has a console)."""
    global _ignoring_ctrl_c
    if os.name != "nt" or not pid:
        return False
    try:
        import ctypes

        kernel32 = _kernel32()
        with _SPAWN_LOCK:
            std_ids = (0xFFFFFFF6, 0xFFFFFFF5, 0xFFFFFFF4)  # STD_INPUT/OUTPUT/ERROR_HANDLE
            saved = [kernel32.GetStdHandle(n) for n in std_ids]
            if kernel32.AttachConsole(pid):
                try:
                    if not _ignoring_ctrl_c:
                        _ignoring_ctrl_c = bool(kernel32.SetConsoleCtrlHandler(None, True))
                    return _ignoring_ctrl_c and bool(kernel32.GenerateConsoleCtrlEvent(_CTRL_C_EVENT, 0))
                finally:
                    kernel32.FreeConsole()
                    # AttachConsole may have replaced empty standard handles
                    # with console handles that FreeConsole just invalidated.
                    for n, handle in zip(std_ids, saved):
                        kernel32.SetStdHandle(n, handle)
                    # The event reaches this process too, a moment later: a
                    # shell starting meanwhile clears "ignore" (above) only
                    # once it has been handled.
                    time.sleep(0.2)
            error = ctypes.get_last_error()
        if error != _ERROR_ACCESS_DENIED or getattr(sys, "frozen", False) or not sys.executable:
            return False  # the shell is gone, or no helper can run
        done = subprocess.run(
            [sys.executable, "-I", "-S", "-c", _CTRL_C_HELPER, str(int(pid))],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=NO_WINDOW, timeout=10,
        )
        return done.returncode == 0
    except Exception:
        return False


def _incomplete_utf8_tail(buf: bytes) -> int:
    """Length of a UTF-8 sequence cut off at the end of *buf* (0 if none)."""
    for back in range(1, min(3, len(buf)) + 1):
        byte = buf[-back]
        if byte < 0x80:
            return 0
        if byte >= 0xC0:
            need = 2 if byte < 0xE0 else 3 if byte < 0xF0 else 4
            return back if need > back else 0
    return 0


class OutputTranscoder:
    """Normalise shell output to UTF-8 bytes.

    Over pipes, cmd and most console tools write the console code page while
    PowerShell (after its UTF-8 switch), git and Python write UTF-8.  Each line
    is kept when it is valid UTF-8 and otherwise decoded with *fallback*, so
    ``dir`` of ``café`` no longer renders as ``caf�``.
    """

    # How long the tail of a character split across two reads may wait for the
    # rest of its bytes before it is emitted anyway.  A shell writes the rest
    # within microseconds, so this only ever fires for genuinely broken output.
    HOLD_GRACE_S = 0.25

    def __init__(self, fallback: str = "oem"):
        try:
            codecs.lookup(fallback)
        except LookupError:
            fallback = "latin-1"
        self.fallback = fallback
        self._held = b""
        self._held_at = 0.0

    def held_expired(self, grace: Optional[float] = None) -> bool:
        """True when held bytes have waited longer than *grace* for their rest."""
        if not self._held:
            return False
        limit = self.HOLD_GRACE_S if grace is None else grace
        return time.monotonic() - self._held_at >= limit

    def feed(self, data: bytes, final: bool = False) -> bytes:
        previous_at = self._held_at
        buf = self._held + data if self._held else data
        self._held = b""
        self._held_at = 0.0
        if not final:
            cut = _incomplete_utf8_tail(buf)
            if cut:
                self._held, buf = buf[-cut:], buf[:-cut]
                # The clock starts when a tail is first held back; an idle poll
                # (no new data) must not keep restarting it.
                self._held_at = time.monotonic() if (data or not previous_at) else previous_at
        if not buf:
            return b""
        try:
            buf.decode("utf-8")
            return buf
        except UnicodeDecodeError:
            pass
        out = []
        for line in buf.splitlines(True):
            try:
                line.decode("utf-8")
                out.append(line)
            except UnicodeDecodeError:
                out.append(line.decode(self.fallback, "replace").encode("utf-8"))
        return b"".join(out)


class LocalShellSession:
    """Interactive subprocess session for local powershell.exe or cmd.exe."""

    # Input waiting for a shell that does not read it (a command runs that
    # never reads its input): more than this is refused, never queued forever.
    INPUT_QUEUE_MAX = 1 << 20

    def __init__(self, shell_type: str = "powershell",
                 serial: Optional[str] = None,
                 cwd: Optional[str] = None,
                 adb_path: Optional[str] = None,
                 *,
                 adb_server_host: Optional[str] = None,
                 adb_server_port: Optional[int] = None,
                 columns: Optional[int] = None):
        self.shell_type = shell_type.lower()
        self.serial = serial
        home = os.path.expanduser("~")
        # A deleted folder must not make the whole shell fail to start.
        self.cwd = cwd if cwd and os.path.isdir(cwd) else home

        # Keep terminal `adb` commands on the same adb as the GUI: the Settings
        # path when the user chose one, else the managed Platform-Tools copy.
        configured_adb = adb_path
        if not configured_adb:
            from .adb_path import gui_adb_path

            configured_adb = gui_adb_path()

        adb_exe = adb_dir = scrcpy_dir = None
        try:
            found = find_adb(configured_adb)
            if found and os.path.exists(found):
                adb_exe = os.path.abspath(found)
                adb_dir = os.path.dirname(adb_exe)
        except Exception:
            pass
        try:
            from ..tools import find_scrcpy

            found = find_scrcpy()
            if found and os.path.exists(found):
                scrcpy_dir = os.path.dirname(os.path.abspath(found))
        except Exception:
            pass

        try:
            registry_env = _get_registry_env()
        except Exception:
            registry_env = {}
        system_root = _system_root()
        self.env = build_shell_env(
            os.environ,
            registry_env=registry_env,
            private_roots=_private_roots(),
            # ADB first so a mismatched adb elsewhere never starts a second
            # daemon; scrcpy (which bundles its own adb) only as a fallback.
            prepend_dirs=[adb_dir],
            append_dirs=[scrcpy_dir],
            serial=serial,
            adb_exe=adb_exe,
            system_root=system_root,
            windows=os.name == "nt",
            adb_server_host=adb_server_host,
            adb_server_port=adb_server_port,
            mark_prompt=True,
        )

        console_codec = _console_codec()
        # cmd reads a pipe one byte at a time in the console code page, so
        # UTF-8 (even after chcp 65001) arrives as U+FFFD.
        self.input_encoding = "utf-8" if self.shell_type == "powershell" else console_codec
        # Set by the owner while a program that reads UTF-8 has the input
        # instead of the shell: an adb shell typed here, whose adb hands every
        # byte to the device as it is (``echo café`` reached it as ``caf\x82``).
        self.utf8_input = False
        self._transcoder = OutputTranscoder(console_codec)

        self._closed = False
        # Input goes out on a writer thread of its own (see send()).
        self._inbox = deque()
        self._inbox_bytes = 0
        self._inbox_cv = threading.Condition()
        self._writer = None
        self._proc = _popen_clean_dll_path(
            shell_argv(self.shell_type, system_root, columns=columns),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=self.cwd,
            env=self.env,
            creationflags=NO_WINDOW,
            bufsize=0,
        )

    @property
    def proc(self) -> subprocess.Popen:
        return self._proc

    @property
    def running(self) -> bool:
        """True until :meth:`close` is called or the shell process exits."""
        return not self._closed and self._proc.poll() is None

    def send(self, data: Union[str, bytes]) -> bool:
        """Queue input for the shell; bytes are UTF-8 (as the console produces
        them), and go out in :attr:`input_encoding` (UTF-8 while
        :attr:`utf8_input` is set).  Returns at once, False when the input was
        refused (the shell is closed, or it has not read
        :attr:`INPUT_QUEUE_MAX` bytes queued already).

        Line breaks go out as LF.  ``pause`` or ``choice`` read one key: of a
        CR LF the LF was left over, and cmd took it for an empty command and
        printed its prompt twice.  Every shell and program reads a line ended
        by LF alone (adb.exe turns CR LF into LF anyway).

        A thread of this session writes the input, in order: a shell that is
        busy with a command does not read it, and once the pipe was full a
        write on the UI thread froze the window until the command ended."""
        encoding = "utf-8" if self.utf8_input else self.input_encoding
        if isinstance(data, str):
            data = data.encode(encoding, "replace")
        elif encoding != "utf-8" and not data.isascii():
            data = data.decode("utf-8", "replace").encode(encoding, "replace")
        data = data.replace(b"\r\n", b"\n")
        proc = self._proc
        if not data or self._closed or proc is None or proc.stdin is None:
            return False
        with self._inbox_cv:
            if self._inbox_bytes + len(data) > self.INPUT_QUEUE_MAX:
                return False
            self._inbox.append(data)
            self._inbox_bytes += len(data)
            if self._writer is None:
                self._writer = threading.Thread(
                    target=self._write_input,
                    args=(proc.stdin,),
                    name="turboadb-local-shell-input",
                    daemon=True,
                )
                self._writer.start()
            self._inbox_cv.notify()
        return True

    def discard_pending_input(self) -> int:
        """Forget input the shell has not been handed yet (Stop, close); returns
        its size.  What is in the pipe already stays there."""
        with self._inbox_cv:
            dropped = self._inbox_bytes
            self._inbox.clear()
            self._inbox_bytes = 0
            self._inbox_cv.notify()
        return dropped

    def _write_input(self, pipe) -> None:
        """Writer thread: hand queued input to the shell, in order.  Ends with
        the session, or once the shell is gone (its kill breaks a blocked write)."""
        while True:
            with self._inbox_cv:
                while not self._inbox and not self._closed:
                    self._inbox_cv.wait()
                if not self._inbox:
                    return
                data = self._inbox.popleft()
                self._inbox_bytes -= len(data)
            try:
                view = memoryview(data)
                while view:
                    written = pipe.write(view)
                    if not written:
                        raise OSError("the shell's input is closed")
                    view = view[written:]
                pipe.flush()
            except (OSError, ValueError):
                self.discard_pending_input()
                return

    def read(self, size: int = 4096) -> bytes:
        """Available output as UTF-8, without hanging on Windows pipe buffers."""
        raw = self._read_raw(size)
        if raw:
            return self._transcoder.feed(raw)
        # Idle pipe.  A byte held back as a possible split character is released
        # only once its rest has clearly not come, or the shell has exited:
        # forcing the final flush on EVERY empty poll defeated the guard, so a
        # character cut at a read boundary was printed as mojibake at once.
        proc = self._proc
        if self._transcoder.held_expired() or proc is None or proc.poll() is not None:
            return self._transcoder.feed(b"", final=True)
        return b""

    def _read_raw(self, size: int) -> bytes:
        try:
            if not self._proc or not self._proc.stdout:
                return b""
            if os.name == "nt":
                h = _msvcrt.get_osfhandle(self._proc.stdout.fileno())
                avail = _wintypes.DWORD()
                if not _PEEK_NAMED_PIPE(h, None, 0, None, _ctypes.byref(avail), None):
                    return b""
                if avail.value == 0:
                    return b""
                return os.read(self._proc.stdout.fileno(), min(size, avail.value))
            return os.read(self._proc.stdout.fileno(), size)
        except Exception:
            return b""

    def send_ctrl_c(self, on_done: Optional[Callable[["CtrlC"], None]] = None) -> bool:
        """Press Ctrl+C in the shell's (hidden) console, without blocking.

        As in a console window, the running command gets it (``ping`` prints
        its summary), and the shell abandons the rest of the command line,
        script or batch file (cmd then asks ``Terminate batch job (Y/N)?``).
        Unsent input goes too.  *on_done* gets a :class:`CtrlC` on the worker
        thread.  False when there is no running shell (or no console: not on
        Windows)."""
        proc = self._proc
        if os.name != "nt" or self._closed or proc is None or proc.poll() is not None:
            return False
        self.discard_pending_input()

        def run():
            programs = _runs_programs(proc.pid)
            result = CtrlC(_console_ctrl_c(proc.pid), programs)
            if on_done is not None:
                try:
                    on_done(result)
                except Exception:
                    pass

        threading.Thread(target=run, name="turboadb-local-shell-ctrl-c", daemon=True).start()
        return True

    def kill_command(self, on_done: Optional[Callable[["CommandStop"], None]] = None) -> bool:
        """Stop the running command and keep the shell (its variables, folder
        and history), without blocking.

        Every process the shell started ends, as a console Ctrl+C would end
        them, but never the shell, its console (``conhost.exe``) or an adb
        server a typed ``adb`` command started (every device tab uses that
        one).  They exit with STATUS_CONTROL_C_EXIT, so cmd prints ``^C`` and,
        in a batch file, asks ``Terminate batch job (Y/N)?`` as for Ctrl+C.
        Unsent input goes too.  *on_done* gets a :class:`CommandStop` on the
        worker thread.  False when there is no running shell."""
        proc = self._proc
        if self._closed or proc is None or proc.poll() is not None:
            return False
        self.discard_pending_input()

        def stop():
            result = _kill_commands(proc.pid)
            if on_done is not None:
                try:
                    on_done(result)
                except Exception:
                    pass

        threading.Thread(target=stop, name="turboadb-local-shell-stop", daemon=True).start()
        return True

    def close(self) -> None:
        """End the shell AND every command it started, without blocking.

        Terminating only powershell/cmd orphaned their children (``adb logcat``,
        ``ping -t`` …), which kept running invisibly.  The process-tree kill
        runs on a daemon thread so closing a tab never waits on it; the pipes
        are closed only after the tree is gone (closing stdin first makes cmd
        exit on EOF before its children can be enumerated).  An adb server a
        typed ``adb`` started lives on (see :func:`_kill_process_tree`).
        """
        if self._closed:
            return
        self._closed = True
        self.discard_pending_input()
        _start_closer(_kill_process_tree, (self._proc,), "turboadb-local-shell-close")

    def interrupt(self) -> None:
        """End the local shell and every command it started, without blocking:
        Stop's last resort, when :meth:`kill_command` left the shell busy (a
        loop inside PowerShell itself).  The widget opens a fresh shell.

        With redirected Windows pipes a literal Ctrl+C byte is only input; it
        is not a console-control event.  The tree kill runs on a daemon thread,
        exactly like :meth:`close`: doing it inline froze the window for
        seconds.
        """
        proc = self._proc
        if not proc or proc.poll() is not None:
            return
        self._closed = True
        self.discard_pending_input()
        _start_closer(_kill_process_tree, (proc, 1.0), "turboadb-local-shell-interrupt")


# Tree kills of closed shells still running (see join_closers).
_CLOSERS = set()
_CLOSERS_LOCK = threading.Lock()


def _start_closer(target, args, name: str) -> None:
    """Run a shell's tree kill on a daemon thread that :func:`join_closers`
    can wait for."""

    def run():
        try:
            target(*args)
        finally:
            with _CLOSERS_LOCK:
                _CLOSERS.discard(thread)

    thread = threading.Thread(target=run, name=name, daemon=True)
    with _CLOSERS_LOCK:
        _CLOSERS.add(thread)
    thread.start()


def join_closers(timeout: float = 3.0) -> int:
    """Wait, *timeout* seconds at most in all, for the shells being closed to
    be gone; returns how many are still going.  For the way out: a daemon
    thread dies with the process, and a shell whose tree kill had not landed
    yet (with its ``ping -t`` or ``adb logcat``) kept running invisibly after
    TurboADB had closed."""
    deadline = time.monotonic() + timeout
    with _CLOSERS_LOCK:
        threads = list(_CLOSERS)
    for thread in threads:
        thread.join(max(0.0, deadline - time.monotonic()))
    return sum(thread.is_alive() for thread in threads)


# What a process ended by Ctrl+C exits with (0xC000013A): cmd then prints ^C
# and, in a batch file, asks whether to end it.
STATUS_CONTROL_C_EXIT = 0xC000013A


class CommandStop(tuple):
    """What :meth:`LocalShellSession.kill_command` did: :attr:`killed`, the
    PIDs it ended (``None`` when the process table could not be read), and
    :attr:`foreground`, whether one of them was the shell's own child."""

    __slots__ = ()

    def __new__(cls, killed: Optional[List[int]], foreground: bool):
        return tuple.__new__(cls, (killed, bool(foreground)))

    @property
    def killed(self) -> Optional[List[int]]:
        return self[0]

    @property
    def foreground(self) -> bool:
        return self[1]


class CtrlC(tuple):
    """What :meth:`LocalShellSession.send_ctrl_c` did: :attr:`sent`, whether
    the Ctrl+C was raised, and :attr:`programs`, whether the shell was
    running programs at the time (False: the shell itself was busy, e.g.
    waiting in ``pause``, ``set /p`` or ``Read-Host``; None: unknown)."""

    __slots__ = ()

    def __new__(cls, sent: bool, programs: Optional[bool]):
        return tuple.__new__(cls, (bool(sent), programs))

    @property
    def sent(self) -> bool:
        return self[0]

    @property
    def programs(self) -> Optional[bool]:
        return self[1]


def _runs_programs(pid: int) -> Optional[bool]:
    """Whether the shell *pid* has child processes besides its console host."""
    try:
        procs = proctree.snapshot()
        if procs is None:
            return None
        return any(info.ppid == pid and info.name != "conhost.exe"
                   for info in proctree.descendants(pid, procs))
    except Exception:
        return None


def _kill_commands(pid: int) -> CommandStop:
    """End what the shell *pid* runs (see :meth:`LocalShellSession.kill_command`)."""
    try:
        procs = proctree.snapshot()
        if procs is None:
            return CommandStop(None, False)
        children = {
            info.pid for info in proctree.descendants(pid, procs)
            if info.ppid == pid and info.name != "conhost.exe"
        }
        killed = proctree.kill_tree(
            pid, include_root=False, skip_names=("conhost.exe",), procs=procs,
            exit_code=STATUS_CONTROL_C_EXIT,
        )
    except Exception:
        return CommandStop(None, False)
    return CommandStop(killed, bool(children & set(killed or ())))


def _close_pipes(proc) -> None:
    for pipe in (proc.stdin, proc.stdout):
        if pipe is None:
            continue
        try:
            pipe.close()
        except OSError:
            pass


def _kill_process_tree(proc, wait_s: float = 2.0) -> None:
    """Kill *proc* and its descendants, then reap it and close its pipes.

    An adb server that a typed ``adb`` command started is a descendant of the
    shell; :func:`turboadb.proctree.kill_tree` spares it (``taskkill /T`` took
    it down, and every device tab lost its shell and logcat at once).
    taskkill, by absolute path, remains the fallback when the process table
    can't be read.  Never raises."""
    try:
        if proc.poll() is None:
            try:
                killed = proctree.kill_tree(proc.pid)
            except Exception:
                killed = None
            if killed is None:
                if os.name == "nt":
                    try:
                        subprocess.run(
                            [_system_exe("taskkill.exe"), "/PID", str(proc.pid), "/T", "/F"],
                            stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            creationflags=NO_WINDOW,
                            timeout=max(2.0, wait_s * 1.5),
                        )
                    except (OSError, subprocess.SubprocessError):
                        pass
                else:
                    proc.terminate()
            try:
                proc.wait(timeout=wait_s)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=wait_s)
    except (OSError, subprocess.SubprocessError):
        try:
            proc.kill()
        except OSError:
            pass
    finally:
        _close_pipes(proc)
