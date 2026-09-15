"""Local PowerShell / Command Prompt terminals behave like a normal Windows shell.

Pure tests cover the child environment (PyInstaller leak stripping, PATH order,
registry expansion) and output transcoding on any OS.  Windows-only tests spawn
the real shells; widget tests need PyQt5 (the ``qapp`` fixture skips without it).
"""
import os
import shutil
import sys
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

windows_only = pytest.mark.skipif(os.name != "nt", reason="spawns cmd.exe / powershell.exe")

MEI = r"C:\Users\me\AppData\Local\Temp\_MEI12345"


@pytest.fixture
def lt():
    return pytest.importorskip("turboadb.gui.local_terminal")


def _path(env):
    return next(v for k, v in env.items() if k.upper() == "PATH").split(";")


def test_frozen_bootloader_state_and_bundle_paths_are_stripped(lt):
    base = {
        "_PYI_APPLICATION_HOME_DIR": MEI,
        "_PYI_ARCHIVE_FILE": r"C:\Apps\TurboADB.exe",
        "_PYI_PARENT_PROCESS_LEVEL": "1",
        "_MEIPASS2": MEI,
        "QT_PLUGIN_PATH": MEI + r"\PyQt5\Qt5\plugins",
        "QML2_IMPORT_PATH": MEI + r"\PyQt5\Qt5\qml",
        "TCL_LIBRARY": MEI + r"\_tcl_data",
        "SSL_CERT_FILE": MEI + r"\certifi\cacert.pem",
        "PYTHONPATH": MEI + r";D:\mylibs",
        "SIBLING": MEI + r"9\not-inside",
        "PATH": MEI + r"\PyQt5\Qt5\bin;" + MEI + r";C:\Tools;C:\Windows\system32",
        "USERPROFILE": r"C:\Users\me",
    }
    env = lt.build_shell_env(base, private_roots=[MEI + "\\"], system_root=r"C:\Windows")

    for gone in ("_PYI_APPLICATION_HOME_DIR", "_PYI_ARCHIVE_FILE", "_PYI_PARENT_PROCESS_LEVEL",
                 "_MEIPASS2", "QT_PLUGIN_PATH", "QML2_IMPORT_PATH", "TCL_LIBRARY", "SSL_CERT_FILE"):
        assert gone not in env
    assert env["PYTHONPATH"] == r"D:\mylibs"
    assert env["SIBLING"] == base["SIBLING"]  # a prefix match is not "inside"
    assert env["USERPROFILE"] == r"C:\Users\me"
    path = _path(env)
    assert not any("_mei12345" in entry.lower() for entry in path)
    assert path[:2] == [r"C:\Tools", r"C:\Windows\system32"]


def test_bootloader_variables_are_stripped_even_when_not_frozen(lt):
    env = lt.build_shell_env({"_PYI_SPLASH_IPC": "1", "PATH": ""})
    assert "_PYI_SPLASH_IPC" not in env


def test_path_order_adb_first_then_original_then_registry_then_system(lt):
    base = {"PATH": r'C:\Git\usr\bin;C:\Python\;"C:\Program Files\Node";c:\python;;'}
    registry = {"Path": r"C:\Windows\system32;C:\NewTool;C:\GIT\USR\BIN"}
    env = lt.build_shell_env(
        base,
        registry_env=registry,
        private_roots=[MEI],
        prepend_dirs=[MEI + r"\adb", None],  # TurboADB's own tools stay even in the bundle
        append_dirs=[r"C:\scrcpy"],
        system_root=r"C:\Windows",
    )
    assert _path(env) == [
        MEI + r"\adb",
        r"C:\Git\usr\bin",
        "C:\\Python\\",
        r"C:\Program Files\Node",
        r"C:\Windows\system32",
        r"C:\NewTool",
        r"C:\Windows",
        r"C:\Windows\System32\Wbem",
        r"C:\Windows\System32\WindowsPowerShell\v1.0",
        r"C:\scrcpy",
    ]


def test_registry_merge_is_case_insensitive_and_inherited_values_win(lt):
    base = {"ONEDRIVE": "env", "Path": r"C:\A"}
    registry = {"OneDrive": "reg", "Brand_New": "v", "PATH": "c:\\a\\;C:\\B"}
    env = lt.build_shell_env(base, registry_env=registry, system_root=r"C:\Windows")
    upper = [key.upper() for key in env]
    assert len(upper) == len(set(upper))
    assert env["ONEDRIVE"] == "env" and env["Brand_New"] == "v"
    assert _path(env)[:2] == [r"C:\A", r"C:\B"]
    assert "Path" in env  # the inherited spelling is kept


def test_launcher_no_cwd_exe_flag_is_dropped_unless_the_registry_sets_it(lt):
    base = {"NODEFAULTCURRENTDIRECTORYINEXEPATH": "1", "PATH": ""}
    assert "NODEFAULTCURRENTDIRECTORYINEXEPATH" not in lt.build_shell_env(base)
    kept = lt.build_shell_env(base, registry_env={"NoDefaultCurrentDirectoryInExePath": "1"})
    assert kept["NODEFAULTCURRENTDIRECTORYINEXEPATH"] == "1"


def test_essential_windows_variables_serial_and_python_encoding(lt):
    env = lt.build_shell_env({}, serial="R58M", adb_exe=r"C:\pt\adb.exe", system_root=r"D:\Win")
    assert env["SystemRoot"] == env["windir"] == r"D:\Win"
    assert env["SystemDrive"] == "D:"
    assert env["ComSpec"] == r"D:\Win\System32\cmd.exe"
    assert ".EXE" in env["PATHEXT"] and ".PS1" not in env["PATHEXT"]
    assert env["ANDROID_SERIAL"] == "R58M" and env["ADB"] == r"C:\pt\adb.exe"
    assert env["PYTHONIOENCODING"] == "utf-8"
    assert "PYTHONIOENCODING" not in lt.build_shell_env({"PYTHONUTF8": "1"})
    assert lt.build_shell_env({"PYTHONIOENCODING": "cp1252"})["PYTHONIOENCODING"] == "cp1252"


def test_registry_expansion_matches_windows(lt):
    REG_SZ, REG_EXPAND_SZ = 1, 2
    system = [
        ("Path", r"%SystemRoot%\system32;%SYSTEMROOT%", REG_EXPAND_SZ),
        ("PATHEXT", ".COM;.EXE", REG_SZ),
    ]
    user = [
        # An EXPAND value listed before the literal it references still sees it.
        ("JAVA_BIN", r"%java_home%\bin", REG_EXPAND_SZ),
        ("JAVA_HOME", r"C:\jdk", REG_SZ),
        ("Path", r"%USERPROFILE%\bin;C:\$dollar\x", REG_EXPAND_SZ),
        ("TEMP", r"%USERPROFILE%\AppData\Local\Temp", REG_EXPAND_SZ),
        ("LITERAL", "%USERPROFILE%", REG_SZ),
        ("UNKNOWN", r"%NOPE%\x", REG_EXPAND_SZ),
        ("Binary", b"\x00", 3),
    ]
    volatile = [("USERPROFILE", r"C:\Users\volatile", REG_SZ), ("APPDATA", r"C:\Users\me\Roaming", REG_SZ)]
    base = {"SYSTEMROOT": r"C:\Windows", "USERPROFILE": r"C:\Users\me"}

    env = lt.expand_registry_env(system, user, volatile, base)

    assert env["Path"] == r"C:\Windows\system32;C:\Windows;C:\Users\me\bin;C:\$dollar\x"
    assert env["TEMP"] == r"C:\Users\me\AppData\Local\Temp"
    assert env["JAVA_BIN"] == r"C:\jdk\bin"
    assert env["LITERAL"] == "%USERPROFILE%"
    assert env["UNKNOWN"] == r"%NOPE%\x"
    assert env["APPDATA"] == r"C:\Users\me\Roaming"
    assert "Binary" not in env


def test_output_transcoder_keeps_utf8_and_decodes_console_code_page(lt):
    tc = lt.OutputTranscoder("cp850")
    utf8 = "café ✓\r\n".encode("utf-8")
    split = utf8.index(b"\xa9")  # inside the two-byte "é"
    assert tc.feed(utf8[:split]) == b"caf"
    assert tc.feed(utf8[split:]) == utf8[3:]

    mixed = "dir café\r\n".encode("cp850") + "git ✓\r\n".encode("utf-8")
    assert tc.feed(mixed).decode("utf-8") == "dir café\r\ngit ✓\r\n"

    # A lone cp850 byte that looks like a UTF-8 lead is released once idle.
    assert tc.feed(b"\xc3") == b""
    assert tc.feed(b"", final=True).decode("utf-8") == "├"


def test_session_env_and_argv(lt, monkeypatch, tmp_path):
    captured = {}

    class FakeProc:
        pid = 1
        stdin = stdout = None

        def poll(self):
            return None

    def fake_popen(argv, **kwargs):
        captured.update(kwargs, argv=argv)
        return FakeProc()

    monkeypatch.setattr(lt.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(lt, "find_adb", lambda *_a, **_k: None)
    monkeypatch.setattr(lt, "_get_registry_env", lambda: {})
    monkeypatch.setattr(lt, "_kill_process_tree", lambda proc, wait_s=2.0: None)
    monkeypatch.setenv("_PYI_PARENT_PROCESS_LEVEL", "1")

    ps = lt.LocalShellSession("powershell", serial="S1", cwd=str(tmp_path / "gone"), adb_path="x")
    assert captured["argv"][-2] == "-Command" and "UTF8Encoding" in captured["argv"][-1]
    assert "-NoExit" in captured["argv"]
    assert captured["cwd"] == os.path.expanduser("~")  # a missing folder falls back
    assert captured["env"] is ps.env and ps.env["ANDROID_SERIAL"] == "S1"
    assert "_PYI_PARENT_PROCESS_LEVEL" not in ps.env
    assert ps.input_encoding == "utf-8"
    ps.close()

    cmd = lt.LocalShellSession("cmd", cwd=str(tmp_path), adb_path="x")
    assert os.path.basename(captured["argv"][0]).lower() == "cmd.exe"
    assert captured["cwd"] == str(tmp_path)
    cmd.close()


# ---------------------------------------------------------------- real shells

def _registry_sets(lt, name):
    return any(key.upper() == name.upper() for key in lt._get_registry_env())


def _drive(lt, shell, cwd, lines, marker, timeout=60.0):
    """Wait for the first prompt, type *lines* like the console does, and
    collect output until *marker* (which the echoed input never contains)."""
    sess = lt.LocalShellSession(shell, cwd=str(cwd), adb_path="__missing__")
    out = ""
    try:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not out.rstrip().endswith(">"):
            chunk = sess.read(65536)
            out += chunk.decode("utf-8", "replace")
            if not chunk:
                time.sleep(0.05)
        for line in lines:
            sess.send((line + "\r\n").encode("utf-8"))
        while time.monotonic() < deadline and marker not in out:
            chunk = sess.read(65536)
            out += chunk.decode("utf-8", "replace")
            if not chunk:
                time.sleep(0.05)
    finally:
        sess.close()
    assert marker in out, out[-2000:]
    return out


@pytest.fixture
def exe_dir(tmp_path):
    """A folder with a copied native exe whose name is on no PATH."""
    shutil.copy(os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "whoami.exe"),
                str(tmp_path / "tadbwho.exe"))
    (tmp_path / "caf\u00e9").mkdir()
    return tmp_path


def _whoami():
    import subprocess

    exe = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "whoami.exe")
    return subprocess.run([exe], capture_output=True, text=True, timeout=30).stdout.strip().lower()


@windows_only
def test_real_cmd_env_vars_current_directory_exe_and_non_ascii(lt, exe_dir, monkeypatch):
    monkeypatch.setenv("TADB_PROBE", "hello-probe")
    monkeypatch.setenv("TADB_END", "__TADB_END__")
    # Agent/IDE launchers set this; a normal cmd window runs tool.exe from cwd.
    monkeypatch.setenv("NoDefaultCurrentDirectoryInExePath", "1")
    out = _drive(lt, "cmd", exe_dir, [
        "echo [%TADB_PROBE%]",
        "tadbwho.exe",
        ".\\tadbwho.exe",
        'cd "caf\u00e9"',
        "echo %TADB_END%",
    ], "__TADB_END__")

    assert "[hello-probe]" in out
    runs = out.lower().count(_whoami())
    if _registry_sets(lt, "NoDefaultCurrentDirectoryInExePath"):
        assert runs >= 1
    else:
        assert runs >= 2, out
    try:
        "caf\u00e9".encode(lt._console_codec())
    except UnicodeEncodeError:
        return  # console code page cannot represent the name at all
    assert str(exe_dir / "caf\u00e9") + ">" in out, out


@windows_only
def test_real_powershell_env_vars_current_directory_exe_and_no_bundle_leak(lt, exe_dir, monkeypatch):
    fake_bundle = exe_dir / "tadb_fake_mei"
    fake_bundle.mkdir()
    monkeypatch.setattr(lt, "_private_roots", lambda: [str(fake_bundle)])
    monkeypatch.setenv("TADB_PROBE", "hello-probe")
    monkeypatch.setenv("QT_PLUGIN_PATH", str(fake_bundle / "plugins"))
    monkeypatch.setenv("_PYI_APPLICATION_HOME_DIR", str(fake_bundle))
    monkeypatch.setenv("PATH", str(fake_bundle) + os.pathsep + os.environ.get("PATH", ""))
    out = _drive(lt, "powershell", exe_dir, [
        'Write-Output ("probe=[" + $env:TADB_PROBE + "]")',
        ".\\tadbwho.exe",
        'Write-Output ("qt=[" + $env:QT_PLUGIN_PATH + "][" + $env:_PYI_APPLICATION_HOME_DIR + "]")',
        'Write-Output ("mei=" + ($env:Path -like "*tadb_fake_mei*"))',
        "Write-Output (\"len=\" + 'caf\u00e9'.Length)",
        "Write-Output ('__TADB' + '_END__')",
    ], "__TADB_END__")

    assert "probe=[hello-probe]" in out
    assert _whoami() in out.lower()
    assert "qt=[][]" in out
    assert "mei=False" in out
    assert "len=4" in out  # typed non-ASCII reaches PowerShell intact


@windows_only
def test_spawn_does_not_pass_on_the_frozen_dll_directory(lt, tmp_path):
    import ctypes
    import subprocess
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.SetDllDirectoryW.argtypes = (wintypes.LPCWSTR,)
    kernel32.GetDllDirectoryW.argtypes = (wintypes.DWORD, wintypes.LPWSTR)
    probe = ("import ctypes;b=ctypes.create_unicode_buffer(1024);"
             "ctypes.windll.kernel32.GetDllDirectoryW(1024,b);print('DLLDIR=['+b.value+']')")
    assert kernel32.SetDllDirectoryW(str(tmp_path))
    try:
        proc = lt._popen_clean_dll_path(
            [sys.executable, "-c", probe],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            creationflags=lt.NO_WINDOW,
        )
        out, _ = proc.communicate(timeout=60)
        buf = ctypes.create_unicode_buffer(1024)
        kernel32.GetDllDirectoryW(1024, buf)
        assert buf.value == str(tmp_path)  # restored for TurboADB itself
    finally:
        kernel32.SetDllDirectoryW(None)
    assert b"DLLDIR=[]" in out, out


# ------------------------------------------------------------------- widget

def test_widget_follows_the_directory_in_the_real_prompt(qapp, tmp_path):
    from turboadb.gui.device_tab import _LocalShellWidget

    ps = _LocalShellWidget("powershell")
    try:
        ps.reader = object()
        ps._feed_from(ps.reader, b"cd $env:TEMP\r\nPS ")
        ps._feed_from(ps.reader, f"{tmp_path}> ".encode("utf-8"))
        assert ps._shell_cwd == str(tmp_path)
        ps._feed_from(ps.reader, b"PS C:\\definitely\\not\\here> ")
        assert ps._shell_cwd == str(tmp_path)
    finally:
        ps.reader = None
        ps.close_panel()

    if os.name == "nt":
        cmd = _LocalShellWidget("cmd")
        try:
            cmd.reader = object()
            cmd._strip_startup_banner = False
            cmd._feed_from(cmd.reader, f"\r\n{tmp_path}>".encode("utf-8"))
            assert cmd._shell_cwd == str(tmp_path)
        finally:
            cmd.reader = None
            cmd.close_panel()


@windows_only
def test_completion_offers_runnable_current_directory_executables(qapp, tmp_path):
    from turboadb.gui.device_tab import _LocalShellWidget

    (tmp_path / "tadbtool.exe").write_bytes(b"")
    (tmp_path / "tadbtoolbox").mkdir()
    ps = _LocalShellWidget("powershell")
    cmd = _LocalShellWidget("cmd")
    try:
        ps._shell_cwd = cmd._shell_cwd = str(tmp_path)
        assert ps._local_complete("tadbto") == (".\\tadbtool.exe ", [])
        assert ps._local_complete(".\\tadbtool.") == (".\\tadbtool.exe", [])
        assert cmd._local_complete("tadbto") == ("tadbtool ", [])
        # _send still passes ordinary commands through untouched.
        sent = []

        class FakeSession:
            running = True
            env = {"PATH": ""}

            def send(self, data):
                sent.append(data)

        ps.session, ps._started = FakeSession(), True
        for line in (b".\\tadbtool.exe --flag\r\n", b"$env:Path -split ';'\r\n", b"whoami\r\n"):
            ps._send(line)
        assert sent == [b".\\tadbtool.exe --flag\r\n", b"$env:Path -split ';'\r\n", b"whoami\r\n"]
    finally:
        ps.session = None
        ps.close_panel()
        cmd.close_panel()
