"""Interactive local terminal session for PowerShell and CMD inside TurboADB.

Binds ANDROID_SERIAL and TurboADB's verified platform-tools path to the subprocess
environment so the user can immediately run adb commands (e.g. adb shell logcat -d)
or arbitrary local scripts directly in the terminal tab.
"""

from __future__ import annotations

import os
import subprocess
from typing import Optional, Union

from ..tools import find_adb, NO_WINDOW


def _get_registry_env() -> dict:
    """Read user and system environment variables directly from Windows Registry."""
    reg_env: dict = {}
    if os.name != "nt":
        return reg_env
    try:
        import winreg

        # 1. System environment
        try:
            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment",
            ) as k:
                num_vals = winreg.QueryInfoKey(k)[1]
                for i in range(num_vals):
                    name, val, _ = winreg.EnumValue(k, i)
                    if isinstance(val, str):
                        reg_env[name] = os.path.expandvars(val)
        except Exception:
            pass

        # 2. User environment
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Environment") as k:
                num_vals = winreg.QueryInfoKey(k)[1]
                for i in range(num_vals):
                    name, val, _ = winreg.EnumValue(k, i)
                    if isinstance(val, str):
                        expanded = os.path.expandvars(val)
                        match_key = next((ek for ek in reg_env if ek.upper() == name.upper()), None)
                        if match_key and name.upper() == "PATH":
                            reg_env[match_key] = reg_env[match_key] + os.pathsep + expanded
                        else:
                            reg_env[name] = expanded
        except Exception:
            pass
    except Exception:
        pass
    return reg_env


class LocalShellSession:
    """Interactive subprocess session for local powershell.exe or cmd.exe."""

    def __init__(self, shell_type: str = "powershell",
                 serial: Optional[str] = None,
                 cwd: Optional[str] = None,
                 adb_path: Optional[str] = None):
        self.shell_type = shell_type.lower()
        self.serial = serial
        self.cwd = cwd or os.path.expanduser("~")

        env = os.environ.copy()

        # Merge registry environment so newly installed tools/variables are detected
        reg_env = _get_registry_env()
        for k, v in reg_env.items():
            if k.upper() == "PATH":
                combined = []
                seen = set()
                for p in (env.get("PATH", "") + os.pathsep + v).split(os.pathsep):
                    p_clean = p.strip().strip('"')
                    if p_clean and p_clean.lower() not in seen:
                        seen.add(p_clean.lower())
                        combined.append(p_clean)
                env["PATH"] = os.pathsep.join(combined)
            elif k not in env:
                env[k] = v

        if serial:
            env["ANDROID_SERIAL"] = serial

        # Resolve verified adb directory and scrcpy directory
        try:
            from . import settings as settings_mod
            configured_adb = adb_path or settings_mod.load().get("adb_path")
        except Exception:
            configured_adb = adb_path

        adb_dir = None
        try:
            adb_exe = find_adb(configured_adb)
            if adb_exe and os.path.exists(adb_exe):
                adb_dir = os.path.dirname(os.path.abspath(adb_exe))
                env["ADB"] = adb_exe
        except Exception:
            pass

        try:
            from ..tools import find_scrcpy
            scrcpy_exe = find_scrcpy()
            if scrcpy_exe and os.path.exists(scrcpy_exe):
                scrcpy_dir = os.path.dirname(os.path.abspath(scrcpy_exe))
                if scrcpy_dir.lower() not in env.get("PATH", "").lower():
                    env["PATH"] = scrcpy_dir + os.pathsep + env.get("PATH", "")
        except Exception:
            pass

        # Ensure critical Windows system directories are unconditionally in PATH
        if os.name == "nt":
            sysroot = os.environ.get("SystemRoot", r"C:\Windows")
            sys_paths = [
                os.path.join(sysroot, "System32"),
                sysroot,
                os.path.join(sysroot, "System32", "Wbem"),
                os.path.join(sysroot, "System32", "WindowsPowerShell", "v1.0"),
            ]
            path_parts = env.get("PATH", "").split(os.pathsep)
            for sp in sys_paths:
                if sp.lower() not in [p.lower() for p in path_parts if p]:
                    path_parts.append(sp)
            env["PATH"] = os.pathsep.join(path_parts)

        # Guarantee TurboADB's verified platform-tools directory is FIRST in PATH
        # ahead of scrcpy directory so we never run mismatched adb daemon versions
        if adb_dir:
            env["PATH"] = adb_dir + os.pathsep + env.get("PATH", "")

        # Pre-ensure adb daemon is alive so `adb devices` in cmd/powershell returns instantly
        try:
            from ..tools import ensure_adb_server
            ensure_adb_server(configured_adb, timeout=5.0)
        except Exception:
            pass

        if "PATHEXT" not in env:
            env["PATHEXT"] = ".COM;.EXE;.BAT;.CMD;.VBS;.VBE;.JS;.JSE;.WSF;.WSH;.MSC;.PS1"

        self._initial_read = True
        if self.shell_type == "powershell":
            cmd = ["powershell.exe", "-NoExit", "-ExecutionPolicy", "Bypass"]
            cmd = ["powershell.exe", "-NoLogo", "-NoExit", "-ExecutionPolicy", "Bypass"]
        else:
            cmd = ["cmd.exe"]



        self._proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=self.cwd,
            env=env,
            creationflags=NO_WINDOW,
            bufsize=0,
        )

    @property
    def proc(self) -> subprocess.Popen:
        return self._proc

    @property
    def running(self) -> bool:
        return self._proc.poll() is None

    def send(self, data: Union[str, bytes]) -> None:
        if isinstance(data, str):
            data = data.encode("utf-8", "replace")
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
        """Read available bytes without hanging on Windows pipe buffers."""
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
        try:
            if self.running:
                self._proc.terminate()
                self._proc.wait(timeout=1.0)
                self._proc.wait(timeout=0.5)
        except Exception:
            try:
                self._proc.kill()
            except Exception:
                pass
        try:
            if self._proc.stdin:
                self._proc.stdin.close()
        except Exception:
            pass
        try:
            if self._proc.stdout:
                self._proc.stdout.close()
        except Exception:
            pass

