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
  IDE/agent terminals set it) is dropped unless the registry sets it, so
  ``tool.exe`` in the current folder runs in cmd as in a normal window.

Redirected pipes use the console code page, not UTF-8: PowerShell is switched to
UTF-8 input/output at startup, cmd input is encoded in the OEM code page, and
output is normalised to UTF-8 by :class:`OutputTranscoder`.
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
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Union

from ..tools import find_adb, NO_WINDOW

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
    if serial:
        put("ANDROID_SERIAL", serial)
    if adb_exe:
        put("ADB", adb_exe)
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


def shell_argv(shell_type: str, system_root: Optional[str] = None) -> List[str]:
    """Absolute shell path, so a ``cmd.exe`` beside TurboADB can't be picked."""
    root = system_root or r"C:\Windows"
    if shell_type == "powershell":
        exe = ntpath.join(root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
        return [
            exe if os.path.isfile(exe) else "powershell.exe",
            "-NoLogo", "-NoExit", "-ExecutionPolicy", "Bypass", "-Command", _PS_UTF8_INIT,
        ]
    exe = ntpath.join(root, "System32", "cmd.exe")
    return [exe if os.path.isfile(exe) else "cmd.exe"]


_SPAWN_LOCK = threading.Lock()


def _popen_clean_dll_path(argv, **kwargs) -> subprocess.Popen:
    """``subprocess.Popen`` without passing on a ``SetDllDirectory`` path.

    PyInstaller's bootloader points the DLL search path at its ``_MEI…`` folder
    and Windows hands that to every child, which then loads TurboADB's bundled
    ucrtbase/VCRUNTIME140/libssl instead of its own.  The path is cleared only
    for the duration of CreateProcess and restored for TurboADB itself.
    """
    if os.name != "nt":
        return subprocess.Popen(argv, **kwargs)
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        get_dir = kernel32.GetDllDirectoryW
        get_dir.argtypes = (wintypes.DWORD, wintypes.LPWSTR)
        get_dir.restype = wintypes.DWORD
        set_dir = kernel32.SetDllDirectoryW
        set_dir.argtypes = (wintypes.LPCWSTR,)
        set_dir.restype = wintypes.BOOL
        buf = ctypes.create_unicode_buffer(32768)
        saved = buf.value if get_dir(len(buf), buf) else ""
    except Exception:
        saved = ""
    if not saved:
        return subprocess.Popen(argv, **kwargs)
    with _SPAWN_LOCK:
        set_dir(None)
        try:
            return subprocess.Popen(argv, **kwargs)
        finally:
            set_dir(saved)


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

    def __init__(self, fallback: str = "oem"):
        try:
            codecs.lookup(fallback)
        except LookupError:
            fallback = "latin-1"
        self.fallback = fallback
        self._held = b""

    def feed(self, data: bytes, final: bool = False) -> bytes:
        buf = self._held + data if self._held else data
        self._held = b""
        if not final:
            cut = _incomplete_utf8_tail(buf)
            if cut:
                self._held, buf = buf[-cut:], buf[:-cut]
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

    def __init__(self, shell_type: str = "powershell",
                 serial: Optional[str] = None,
                 cwd: Optional[str] = None,
                 adb_path: Optional[str] = None):
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
        )

        console_codec = _console_codec()
        # cmd reads a pipe one byte at a time in the console code page, so
        # UTF-8 (even after chcp 65001) arrives as U+FFFD.
        self.input_encoding = "utf-8" if self.shell_type == "powershell" else console_codec
        self._transcoder = OutputTranscoder(console_codec)

        self._closed = False
        self._proc = _popen_clean_dll_path(
            shell_argv(self.shell_type, system_root),
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

    def send(self, data: Union[str, bytes]) -> None:
        """Write input; bytes are UTF-8 (as the console produces them)."""
        if isinstance(data, str):
            data = data.encode(self.input_encoding, "replace")
        elif self.input_encoding != "utf-8" and not data.isascii():
            data = data.decode("utf-8", "replace").encode(self.input_encoding, "replace")
        try:
            if self._proc and self._proc.stdin:
                if data.endswith(b"\n") and not data.endswith(b"\r\n"):
                    data = data[:-1] + b"\r\n"
                self._proc.stdin.write(data)
                self._proc.stdin.flush()
        except Exception:
            pass

    def send_line(self, line: str) -> None:
        self.send(line + "\r\n")

    def read(self, size: int = 4096) -> bytes:
        """Available output as UTF-8, without hanging on Windows pipe buffers."""
        raw = self._read_raw(size)
        if raw:
            return self._transcoder.feed(raw)
        # Idle pipe: release a byte held back as a possible split character.
        return self._transcoder.feed(b"", final=True)

    def _read_raw(self, size: int) -> bytes:
        try:
            if not self._proc or not self._proc.stdout:
                return b""
            if os.name == "nt":
                import ctypes
                import msvcrt
                from ctypes import wintypes
                h = msvcrt.get_osfhandle(self._proc.stdout.fileno())
                avail = wintypes.DWORD()
                if not ctypes.windll.kernel32.PeekNamedPipe(h, None, 0, None, ctypes.byref(avail), None):
                    return b""
                if avail.value == 0:
                    return b""
                return os.read(self._proc.stdout.fileno(), min(size, avail.value))
            return os.read(self._proc.stdout.fileno(), size)
        except Exception:
            return b""

    def close(self) -> None:
        """End the shell AND every command it started, without blocking.

        Terminating only powershell/cmd orphaned their children (``adb logcat``,
        ``ping -t`` …), which kept running invisibly.  The process-tree kill
        runs on a daemon thread so closing a tab never waits on taskkill; the
        pipes are closed only after the tree is gone (closing stdin first makes
        cmd exit on EOF before its children can be enumerated).
        """
        if self._closed:
            return
        self._closed = True
        threading.Thread(
            target=_kill_process_tree,
            args=(self._proc,),
            name="turboadb-local-shell-close",
            daemon=True,
        ).start()

    def interrupt(self) -> None:
        """Stop the local shell and every command it started.

        With redirected Windows pipes a literal Ctrl+C byte is only input; it
        is not a console-control event.  The embedded terminal therefore ends
        the shell process tree and its widget opens a clean replacement shell.
        """
        proc = self._proc
        if not proc or proc.poll() is not None:
            return
        _kill_process_tree(proc, wait_s=1.0)


def _close_pipes(proc) -> None:
    for pipe in (proc.stdin, proc.stdout):
        if pipe is None:
            continue
        try:
            pipe.close()
        except OSError:
            pass


def _kill_process_tree(proc, wait_s: float = 2.0) -> None:
    """Kill *proc* and its descendants (``taskkill /T /F`` on Windows), then
    reap it and close its pipes.  Never raises."""
    try:
        if proc.poll() is None:
            if os.name == "nt":
                try:
                    subprocess.run(
                        ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
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
