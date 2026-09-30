"""TurboADB.exe: the GUI in a Python that Windows knows as TurboADB.

Windows names a process after its program file.  The GUI ran in pythonw.exe,
so Task Manager listed it as "Python" with Python's icon, and a taskbar pin
started a bare pythonw.  Now the GUI runs in a copy of this Python's
pythonw.exe that carries TurboADB's name, icon and version, beside copies of
the Python DLLs and a ``pyvenv.cfg`` pointing at this Python's standard
library: the layout of a virtualenv made with copies.  A generated module
(``TurboADB.exe -m turboadb_launch``) puts this environment's ``sys.path``
back in place (a source checkout first, as ``python main.py`` has it) and sets
``sys.executable`` back to this interpreter, so pip, the update checks and
every helper process still run the real Python.  Only the GUI's own process
is TurboADB.

The copy lives in ``~/.turboadb/launcher/<environment>/`` and is rebuilt when
Python, TurboADB or the icon change; each build is checked by starting it once.
When anything fails the GUI runs in-process as before, and
``TURBOADB_NO_LAUNCHER=1`` keeps it there (the test suite sets it).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
import time
from typing import Dict, List, NamedTuple, Optional, Tuple

from .config import user_path

EXE_NAME = "TurboADB.exe"
_MODULE = "turboadb_launch"  # TurboADB.exe -m turboadb_launch starts the GUI
_FORMAT = 1  # the layout and script generation: a change rebuilds every copy
_EARLY_EXIT_S = 1.5  # a GUI that ended this soon did not start: run it here
_IN_USE_WAIT_S = 8.0  # for the TurboADB.exe an update is closing
_PROBE_TIMEOUT_S = 30.0
_BREAKAWAY = 0x01000000  # CREATE_BREAKAWAY_FROM_JOB
_OPT_OUT = "TURBOADB_NO_LAUNCHER"
_MARK = "TURBOADB-LAUNCHER:"
RT_ICON, RT_GROUP_ICON, RT_VERSION = 3, 14, 16

BRANDED = False  # this process is TurboADB.exe (set by run_branded)
_HOME: Optional[str] = None


class Runtime(NamedTuple):
    home: str  # ~/.turboadb/launcher/<environment>
    exe: str  # its bin/TurboADB.exe
    script: str  # its Lib/site-packages/turboadb_launch.py

    @property
    def args(self) -> List[str]:
        """TurboADB.exe's arguments for the GUI.  A module of the copy's own
        site folder, not a script's path: a pin's command may hold only 255
        characters, and two long paths overran that."""
        return ["-m", _MODULE]


def _runtime_at(home: str) -> Runtime:
    return Runtime(home, os.path.join(home, "bin", EXE_NAME),
                   os.path.join(home, "Lib", "site-packages", _MODULE + ".py"))


def _base_dir() -> str:
    """The folder of the Python installation this interpreter (or its venv) runs."""
    exe = getattr(sys, "_base_executable", None) or sys.executable or ""
    return os.path.dirname(os.path.abspath(exe))


def supported() -> bool:
    """Whether the GUI can run as TurboADB.exe: Windows, a Python (not the
    one-file exe, which is TurboADB already) with a pythonw.exe to copy, and
    not turned off with ``TURBOADB_NO_LAUNCHER``."""
    if sys.platform != "win32" or getattr(sys, "frozen", False):
        return False
    if os.environ.get(_OPT_OUT, "").strip() not in ("", "0"):
        return False
    return os.path.isfile(os.path.join(_base_dir(), "pythonw.exe"))


# ----------------------------------------------------------------- the copy
def _origin() -> Dict[str, object]:
    """What the copy must reproduce of this process: its interpreter, the
    folder holding the running ``turboadb`` package, ``sys.path`` and the
    site folders whose ``.pth`` files (editable installs) must run again."""
    import site

    import turboadb
    from .tools import windowless_python

    root = os.path.dirname(os.path.dirname(os.path.abspath(turboadb.__file__)))
    # sys.path[0] is where this process was started from (main.py's folder, a
    # launcher, the current folder of -m).  An installed package is found in
    # its site folder, after the standard library; a source checkout goes
    # first, as python main.py has it.
    rest = [os.path.abspath(p) for p in sys.path[1:] if p]
    installed = os.path.normcase(root) in {os.path.normcase(p) for p in rest}
    path = list(dict.fromkeys(rest if installed else [root] + rest))
    sites: List[str] = []
    try:
        sites += site.getsitepackages()
    except Exception:
        pass
    if site.ENABLE_USER_SITE:
        sites.append(site.getusersitepackages())
    sites = [s for s in dict.fromkeys(os.path.abspath(s) for s in sites) if os.path.isdir(s)]
    return {"executable": windowless_python(), "root": root, "path": path, "sites": sites}


def _key(origin: Dict[str, object]) -> str:
    """The copy's folder name: one per Python, environment and TurboADB copy."""
    raw = "|".join((_base_dir(), os.path.abspath(sys.prefix), str(origin["root"]))).lower()
    return "py%d%d-%s" % (sys.version_info[0], sys.version_info[1],
                          hashlib.sha1(raw.encode("utf-8")).hexdigest()[:10])


def _python_files(base: str) -> List[str]:
    """The DLLs a copy of pythonw.exe needs beside it (python3X, the stable
    ABI's python3.dll that PyQt5 loads, the C runtime)."""
    names = []
    for name in sorted(os.listdir(base)):
        low = name.lower()
        if low.endswith(".dll") and not low.endswith("_d.dll") and low.startswith(("python3", "vcruntime140")):
            names.append(name)
    return names


def _stamp() -> Dict[str, object]:
    """What the program file is made of: a change rebuilds it."""
    from . import __version__
    from .winshell import asset

    base = _base_dir()
    files = {}
    for name in ["pythonw.exe"] + _python_files(base):
        st = os.stat(os.path.join(base, name))
        files[name] = [st.st_size, int(st.st_mtime)]
    with open(asset("icon.ico"), "rb") as fh:
        icon = hashlib.sha1(fh.read()).hexdigest()
    return {"format": _FORMAT, "version": __version__, "icon": icon, "files": files}


_SCRIPT = '''\
# TurboADB's GUI in TurboADB.exe, for {root!r} on Python {python}.
# Generated by turboadb.launcher: edits are overwritten.
import os
import site
import sys

HOME = {home!r}
EXECUTABLE = {executable!r}
PATH = {path!r}
SITES = {sites!r}

for _dir in SITES:
    if os.path.isdir(_dir) and _dir not in sys.path:
        site.addsitedir(_dir)
sys.path[:] = list(dict.fromkeys([p for p in PATH if p in sys.path or os.path.isdir(p)] + sys.path))
sys.executable = EXECUTABLE

from turboadb import launcher  # noqa: E402

sys.exit(launcher.run_branded(sys.argv[1:], HOME))
'''


def _script_text(origin: Dict[str, object], home: str) -> str:
    return _SCRIPT.format(python="%d.%d.%d" % sys.version_info[:3], home=home, **origin)


def _cfg_text() -> str:
    return (f"home = {_base_dir()}\n"
            "include-system-site-packages = false\n"
            f"version = {'%d.%d.%d' % sys.version_info[:3]}\n")


def _read_state(home: str) -> dict:
    try:
        with open(os.path.join(home, "state.json"), encoding="utf-8") as fh:
            state = json.load(fh)
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_text(path: str, text: str) -> None:
    try:
        with open(path, encoding="utf-8") as fh:
            if fh.read() == text:
                return
    except OSError:
        pass
    tmp = path + ".new"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


class _Lock:
    """One build at a time per copy (two launches at once, the GUI's shortcut
    worker): an exclusive lock on a byte of ``home/.lock``."""

    def __init__(self, home: str):
        self.path = os.path.join(home, ".lock")

    def __enter__(self):
        import msvcrt

        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.fh = open(self.path, "a+b")
        deadline = time.monotonic() + 60
        while True:
            try:
                self.fh.seek(0)
                msvcrt.locking(self.fh.fileno(), msvcrt.LK_NBLCK, 1)
                return self
            except OSError:
                if time.monotonic() > deadline:
                    self.fh.close()
                    raise
                time.sleep(0.1)

    def __exit__(self, *exc):
        import msvcrt

        try:
            self.fh.seek(0)
            msvcrt.locking(self.fh.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        self.fh.close()


def _replace(new: str, dst: str, wait: float = _IN_USE_WAIT_S) -> None:
    """Move *new* over *dst*, waiting a moment while *dst* is in use (the
    TurboADB.exe an update is closing as the updated one starts)."""
    deadline = time.monotonic() + wait
    while True:
        try:
            os.replace(new, dst)
            return
        except PermissionError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.25)


def _same_file(a: str, b: str) -> bool:
    try:
        sa, sb = os.stat(a), os.stat(b)
    except OSError:
        return False
    return sa.st_size == sb.st_size and int(sa.st_mtime) == int(sb.st_mtime)


def _build(rt: Runtime) -> None:
    """Copy pythonw.exe as TurboADB.exe with TurboADB's icon and version, and
    the DLLs beside it (those that changed).  Raises OSError when TurboADB.exe
    stays in use (it is replaced at a later start)."""
    from . import __version__
    from .winshell import asset

    base = _base_dir()
    bin_dir = os.path.dirname(rt.exe)
    os.makedirs(bin_dir, exist_ok=True)
    source = os.path.join(base, "pythonw.exe")
    new = rt.exe + ".new"
    try:
        shutil.copy2(source, new)
        _brand(new, asset("icon.ico"), _version_strings(source), _version_numbers(__version__))
        _replace(new, rt.exe)
    finally:
        if os.path.exists(new):
            os.remove(new)
    for name in _python_files(base):
        src, dst = os.path.join(base, name), os.path.join(bin_dir, name)
        if not _same_file(src, dst):
            shutil.copy2(src, dst + ".new")
            _replace(dst + ".new", dst)


def _forget_orphans(keep: str) -> None:
    """Remove the copies made for Python installations that are gone (a
    ``pyvenv.cfg`` whose ``home`` no longer holds a pythonw.exe)."""
    root = os.path.dirname(keep)
    try:
        names = os.listdir(root)
    except OSError:
        return
    for name in names:
        home = os.path.join(root, name)
        if os.path.normcase(home) == os.path.normcase(keep):
            continue
        try:
            with open(os.path.join(home, "pyvenv.cfg"), encoding="utf-8") as fh:
                base = next((line.split("=", 1)[1].strip() for line in fh
                             if line.split("=", 1)[0].strip() == "home"), "")
        except (OSError, IndexError):
            continue
        if base and not os.path.isfile(os.path.join(base, "pythonw.exe")):
            shutil.rmtree(home, ignore_errors=True)


def _probe(rt: Runtime) -> bool:
    """Start the copy once: it must run, as TurboADB.exe, and import this very
    ``turboadb`` (and PyQt5, when this Python has it)."""
    import importlib.util

    import turboadb

    qt = importlib.util.find_spec("PyQt5") is not None
    try:
        r = subprocess.run([rt.exe] + rt.args + ["--probe"] + (["--qt"] if qt else []),
                           capture_output=True, text=True, timeout=_PROBE_TIMEOUT_S, cwd=rt.home)
    except Exception:
        return False
    report = None
    for line in (r.stdout or "").splitlines():
        if line.startswith(_MARK):
            try:
                report = json.loads(line[len(_MARK):])
            except ValueError:
                pass
    if not isinstance(report, dict):
        return False

    def same(a, b):
        return os.path.normcase(os.path.abspath(str(a))) == os.path.normcase(os.path.abspath(str(b)))

    return (same(report.get("turboadb"), turboadb.__file__) and same(report.get("image"), rt.exe)
            and (report.get("qt") is True or not qt))


def runtime(build: bool = True) -> Optional[Runtime]:
    """This TurboADB's TurboADB.exe, built or brought up to date first (with
    *build* False: only one that is ready now, for the UI thread).  None when
    the GUI can't run that way here."""
    if BRANDED and _HOME:
        return _runtime_at(_HOME)
    if not supported():
        return None
    try:
        origin = _origin()
        rt = _runtime_at(user_path("launcher", _key(origin)))
        script, cfg = _script_text(origin, rt.home), _cfg_text()
        wanted = {"stamp": _stamp(), "files": hashlib.sha1((script + cfg).encode("utf-8")).hexdigest()}
        state = _read_state(rt.home)
        if state.get("built") == wanted and os.path.isfile(rt.exe):
            return rt if state.get("ok") else None
        if not build:
            return None
        with _Lock(rt.home):
            state = _read_state(rt.home)
            if state.get("built") == wanted and os.path.isfile(rt.exe):
                return rt if state.get("ok") else None  # built meanwhile
            if (state.get("built") or {}).get("stamp") != wanted["stamp"] or not os.path.isfile(rt.exe):
                _build(rt)
            _write_text(os.path.join(rt.home, "pyvenv.cfg"), cfg)
            os.makedirs(os.path.dirname(rt.script), exist_ok=True)
            _write_text(rt.script, script)
            ok = _probe(rt)
            _write_text(os.path.join(rt.home, "state.json"), json.dumps({"built": wanted, "ok": ok}))
        _forget_orphans(rt.home)
        return rt if ok else None
    except Exception:
        return None


def gui_command(build: bool = True) -> Optional[Tuple[str, List[str], str]]:
    """``(program, arguments, folder)`` that start this TurboADB's GUI as
    TurboADB.exe, or None when it can't run that way (see :func:`runtime`)."""
    rt = runtime(build=build)
    return (rt.exe, rt.args, rt.home) if rt else None


def relaunch(argv) -> Optional[int]:
    """Start the GUI as TurboADB.exe; returns the exit code the caller ends
    with, or None when the GUI should run in this process instead: not on
    Windows, turned off, under a debugger, or the copy could not be made or
    ended at once."""
    if BRANDED or sys.gettrace() is not None or not supported():
        return None
    rt = runtime()
    if rt is None:
        return None
    env = {k: v for k, v in os.environ.items() if k != "__PYVENV_LAUNCHER__"}
    cmd = [rt.exe] + rt.args + [str(a) for a in argv]
    options = dict(cwd=rt.home, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, close_fds=True)
    try:
        proc = subprocess.Popen(cmd, creationflags=_BREAKAWAY, **options)
        free = True
    except OSError:  # a job that allows no breakaway
        try:
            proc = subprocess.Popen(cmd, **options)
        except OSError:
            return None
        free = False
    try:
        code = proc.wait(timeout=_EARLY_EXIT_S)
    except subprocess.TimeoutExpired:
        # it runs.  Inside a job that ends its processes with this one (an IDE,
        # a launcher that allows no breakaway), stay: the GUI would go too
        return 0 if free else proc.wait()
    return 0 if code == 0 else None


def run_branded(argv: List[str], home: str) -> int:
    """TurboADB.exe's work, called by its generated script: the GUI, or with
    ``--probe`` the report a build is checked with."""
    global BRANDED, _HOME
    BRANDED, _HOME = True, home
    if argv[:1] == ["--probe"]:
        return _report("--qt" in argv)
    from .gui.app import main

    return main()


def _report(qt: bool) -> int:
    import ctypes

    import turboadb

    buf = ctypes.create_unicode_buffer(32768)
    ctypes.windll.kernel32.GetModuleFileNameW(None, buf, len(buf))
    out = {"turboadb": turboadb.__file__, "image": buf.value, "executable": sys.executable}
    if qt:
        try:
            import PyQt5.QtWidgets  # noqa: F401

            out["qt"] = True
        except Exception as exc:
            out["qt"] = repr(exc)
    print(_MARK + json.dumps(out), flush=True)
    return 0


# ------------------------------------------------ icon and version resources
def _version_numbers(version: str) -> Tuple[int, int, int, int]:
    nums = []
    for part in version.split(".")[:4]:
        digits = ""
        for ch in part:
            if not ch.isdigit():
                break
            digits += ch
        nums.append(int(digits or 0))
    return tuple((nums + [0, 0, 0, 0])[:4])  # type: ignore[return-value]


def _version_strings(source: str) -> Dict[str, str]:
    """TurboADB's version strings; Python's copyright stays with its runtime."""
    from . import __version__

    python = "%d.%d.%d" % sys.version_info[:3]
    return {
        "CompanyName": "TurboADB",
        "FileDescription": "TurboADB",  # Task Manager's name for the process
        "FileVersion": __version__,
        "InternalName": "TurboADB",
        "LegalCopyright": _file_version_string(source, "LegalCopyright")
        or "Python: Copyright (c) Python Software Foundation",
        "OriginalFilename": EXE_NAME,
        "ProductName": "TurboADB",
        "ProductVersion": __version__,
        "Comments": f"The TurboADB desktop app, run by Python {python}",
    }


def _file_version_string(path: str, key: str) -> Optional[str]:
    try:
        import ctypes
        from ctypes import wintypes

        ver = ctypes.WinDLL("version")
        ver.GetFileVersionInfoSizeW.argtypes = (wintypes.LPCWSTR, ctypes.c_void_p)
        ver.GetFileVersionInfoW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p)
        ver.VerQueryValueW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p),
                                       ctypes.POINTER(wintypes.UINT))
        size = ver.GetFileVersionInfoSizeW(path, None)
        if not size:
            return None
        data = ctypes.create_string_buffer(size)
        if not ver.GetFileVersionInfoW(path, 0, size, data):
            return None
        ptr, n = ctypes.c_void_p(), wintypes.UINT()
        if not ver.VerQueryValueW(data, "\\VarFileInfo\\Translation", ctypes.byref(ptr), ctypes.byref(n)) \
                or n.value < 4:
            return None
        lang, codepage = struct.unpack("<HH", ctypes.string_at(ptr, 4))
        if not ver.VerQueryValueW(data, f"\\StringFileInfo\\{lang:04x}{codepage:04x}\\{key}",
                                  ctypes.byref(ptr), ctypes.byref(n)) or not n.value:
            return None
        return ctypes.wstring_at(ptr, n.value).rstrip("\0") or None
    except Exception:
        return None


def _ico_images(path: str) -> List[Tuple[Tuple[int, int, int, int, int], bytes]]:
    """The images of an ``.ico`` file: ((width, height, colours, planes, bits), data)."""
    with open(path, "rb") as fh:
        data = fh.read()
    _reserved, kind, count = struct.unpack_from("<HHH", data, 0)
    if kind != 1 or not count:
        raise ValueError(f"{path} is not an icon")
    images = []
    for i in range(count):
        w, h, colors, _r, planes, bits, size, offset = struct.unpack_from("<BBBBHHII", data, 6 + 16 * i)
        images.append(((w, h, colors, planes, bits), data[offset:offset + size]))
    return images


def _node(key: str, value: bytes = b"", children=(), text: bool = True, value_len=None) -> bytes:
    """One block of a VS_VERSIONINFO resource: header, key, value, children,
    each starting on a 4-byte boundary."""
    kind = 1 if text else 0
    data = struct.pack("<HHH", 0, 0, kind) + (key + "\0").encode("utf-16-le")
    data += b"\0" * (-len(data) % 4)
    data += value
    for child in children:
        data += b"\0" * (-len(data) % 4)
        data += child
    length = len(value) if value_len is None else value_len
    return struct.pack("<HHH", len(data), length, kind) + data[6:]


def _version_resource(strings: Dict[str, str], version: Tuple[int, int, int, int],
                      lang: int = 0x0409, codepage: int = 0x04B0) -> bytes:
    ms, ls = (version[0] << 16) | version[1], (version[2] << 16) | version[3]
    fixed = struct.pack("<13I", 0xFEEF04BD, 0x00010000, ms, ls, ms, ls,
                        0x3F, 0, 0x40004, 1, 0, 0, 0)  # VOS_NT_WINDOWS32, VFT_APP
    table = _node(f"{lang:04x}{codepage:04x}", children=[
        _node(k, (v + "\0").encode("utf-16-le"), value_len=len(v) + 1) for k, v in strings.items()])
    return _node("VS_VERSION_INFO", fixed, [
        _node("StringFileInfo", children=[table]),
        _node("VarFileInfo", children=[_node("Translation", struct.pack("<HH", lang, codepage), text=False)]),
    ], text=False)


def _resources(path: str, types) -> List[Tuple[int, object, int]]:
    """``(type, name, language)`` of the resources of these *types* in *path*
    (a name is an int ID or a string)."""
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.LoadLibraryExW.restype = wintypes.HMODULE
    k32.LoadLibraryExW.argtypes = (wintypes.LPCWSTR, wintypes.HANDLE, wintypes.DWORD)
    k32.FreeLibrary.argtypes = (wintypes.HMODULE,)
    k32.EnumResourceNamesW.argtypes = (wintypes.HMODULE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)
    k32.EnumResourceLanguagesW.argtypes = (wintypes.HMODULE, ctypes.c_void_p, ctypes.c_void_p,
                                           ctypes.c_void_p, ctypes.c_void_p)
    names_cb = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HMODULE, ctypes.c_void_p, ctypes.c_void_p,
                                  ctypes.c_void_p)
    langs_cb = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HMODULE, ctypes.c_void_p, ctypes.c_void_p,
                                  wintypes.WORD, ctypes.c_void_p)
    module = k32.LoadLibraryExW(path, None, 0x2)  # LOAD_LIBRARY_AS_DATAFILE
    if not module:
        raise ctypes.WinError(ctypes.get_last_error())
    found = []
    try:
        for rtype in types:
            names = []

            @names_cb
            def on_name(_m, _t, name, _p, names=names):
                names.append(name if name < 0x10000 else ctypes.wstring_at(name))
                return True

            k32.EnumResourceNamesW(module, ctypes.c_void_p(rtype), on_name, None)
            for name in names:
                langs = []

                @langs_cb
                def on_lang(_m, _t, _n, lang, _p, langs=langs):
                    langs.append(lang)
                    return True

                ref = ctypes.c_void_p(name) if isinstance(name, int) else ctypes.c_wchar_p(name)
                k32.EnumResourceLanguagesW(module, ctypes.c_void_p(rtype), ctypes.cast(ref, ctypes.c_void_p),
                                           on_lang, None)
                found += [(rtype, name, lang) for lang in langs]
    finally:
        k32.FreeLibrary(module)
    return found


def _brand(exe: str, ico: str, strings: Dict[str, str], version: Tuple[int, int, int, int]) -> None:
    """Replace the icons and the version information of the program file *exe*
    (its manifest and everything else stay)."""
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.BeginUpdateResourceW.restype = wintypes.HANDLE
    k32.BeginUpdateResourceW.argtypes = (wintypes.LPCWSTR, wintypes.BOOL)
    k32.UpdateResourceW.restype = wintypes.BOOL
    k32.UpdateResourceW.argtypes = (wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, wintypes.WORD,
                                    ctypes.c_void_p, wintypes.DWORD)
    k32.EndUpdateResourceW.restype = wintypes.BOOL
    k32.EndUpdateResourceW.argtypes = (wintypes.HANDLE, wintypes.BOOL)
    old = _resources(exe, (RT_ICON, RT_GROUP_ICON, RT_VERSION))
    images = _ico_images(ico)
    handle = k32.BeginUpdateResourceW(exe, False)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    keep = []  # the buffers must live until EndUpdateResource

    def update(rtype, name, lang, data):
        if data is None:
            ref = ctypes.c_void_p(name) if isinstance(name, int) else ctypes.c_wchar_p(name)
            keep.append(ref)
            ok = k32.UpdateResourceW(handle, rtype, ctypes.cast(ref, ctypes.c_void_p), lang, None, 0)
        else:
            buf = ctypes.create_string_buffer(data, len(data))
            keep.append(buf)
            ok = k32.UpdateResourceW(handle, rtype, name, lang, buf, len(data))
        if not ok:
            raise ctypes.WinError(ctypes.get_last_error())

    try:
        for rtype, name, lang in old:
            update(rtype, name, lang, None)
        group = struct.pack("<HHH", 0, 1, len(images))
        for n, ((w, h, colors, planes, bits), blob) in enumerate(images, 1):
            update(RT_ICON, n, 0x0409, blob)
            group += struct.pack("<BBBBHHIH", w, h, colors, 0, planes, bits, len(blob), n)
        update(RT_GROUP_ICON, 1, 0x0409, group)
        update(RT_VERSION, 1, 0x0409, _version_resource(strings, version))
    except Exception:
        k32.EndUpdateResourceW(handle, True)  # discard
        raise
    if not k32.EndUpdateResourceW(handle, False):
        raise ctypes.WinError(ctypes.get_last_error())
