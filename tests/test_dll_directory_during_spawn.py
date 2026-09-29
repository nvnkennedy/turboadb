"""Starting a PowerShell/CMD terminal clears the process's SetDllDirectory
path for a moment (the one-file exe's bundle folder must not reach the
shell).  Its folder stays on the DLL search path for TurboADB's own threads
meanwhile, through AddDllDirectory, so an import on another thread still
finds the bundled DLLs."""

import os
import subprocess

import pytest

from turboadb.gui import local_terminal as lt


@pytest.mark.skipif(os.name != "nt", reason="SetDllDirectory is Windows-only")
def test_the_bundle_folder_stays_searchable_while_a_shell_starts(monkeypatch, tmp_path):
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.SetDllDirectoryW.argtypes = (wintypes.LPCWSTR,)
    kernel32.GetDllDirectoryW.argtypes = (wintypes.DWORD, wintypes.LPWSTR)

    def current():
        buf = ctypes.create_unicode_buffer(32768)
        return buf.value if kernel32.GetDllDirectoryW(len(buf), buf) else ""

    kept = []
    monkeypatch.setattr(lt, "_KEPT_DLL_DIRS", {})
    monkeypatch.setattr(os, "add_dll_directory", lambda path: kept.append(path) or path,
                        raising=False)
    seen_during = []
    real_popen = subprocess.Popen

    def popen(*args, **kwargs):
        seen_during.append(current())  # cleared while the child is created
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(lt.subprocess, "Popen", popen)
    bundle = str(tmp_path)
    assert kernel32.SetDllDirectoryW(bundle)
    try:
        for _ in range(2):
            proc = lt._popen_clean_dll_path(
                [lt._system_exe("cmd.exe"), "/c", "exit 0"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, creationflags=lt.NO_WINDOW,
            )
            assert proc.wait(30) == 0
        assert seen_during == ["", ""]
        assert current() == bundle  # restored for TurboADB itself
        assert kept == [bundle]  # added once, for good
    finally:
        kernel32.SetDllDirectoryW(None)
