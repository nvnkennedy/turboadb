"""TurboADB on the Windows taskbar and in the shell, through ctypes COM.

* :func:`taskbar_is_light` and :func:`icon_file`: the taskbar's mode (Settings >
  Personalization > Colors) and the icon that goes with it;
* :func:`set_window_identity`: a window's AppUserModel properties, its app ID
  and the command, name and icon a taskbar pin relaunches it with;
* :func:`read_shortcut`, :func:`write_shortcut`, :func:`set_shortcut_icon`:
  ``.lnk`` files with that app ID, so a Start-menu, Desktop or taskbar pin
  groups with the running window instead of showing beside it.

Windows only, and nothing here raises: a failure returns None or False, and
the caller carries on without it (the frozen exe and ``pythonw`` did before).
"""

from __future__ import annotations

import ntpath
import os
import sys
import uuid
from typing import Dict, Iterable, List, Optional

from .config import user_path

APP_ID = "TurboADB.DeviceToolkit"
APP_NAME = "TurboADB"
_PERSONALIZE = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"


def taskbar_is_light() -> bool:
    """Whether Windows shows a light taskbar ("Choose your mode": Light, or
    Custom with a light Windows mode).  Windows 10 without the setting: dark."""
    if sys.platform != "win32":
        return False
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _PERSONALIZE) as key:
            value, _kind = winreg.QueryValueEx(key, "SystemUsesLightTheme")
        return bool(value)
    except (OSError, ValueError):
        return False


def asset(name: str) -> str:
    """A file of ``turboadb/assets`` (inside the one-file exe's temp folder too)."""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", name)


def icon_file(light: Optional[bool] = None) -> str:
    """The icon for a light or dark taskbar (*light* None: the current one),
    as a lasting copy in ``~/.turboadb/icons``: shortcuts and pins point at
    it, and a pip upgrade or the one-file exe's temp folder would pull the
    package's own copy from under them.  The package's file when no copy can
    be made."""
    if light is None:
        light = taskbar_is_light()
    src = asset("icon-light.ico" if light else "icon.ico")
    dst = user_path("icons", "TurboADB-light.ico" if light else "TurboADB-dark.ico")
    try:
        with open(src, "rb") as fh:
            data = fh.read()
        try:
            with open(dst, "rb") as fh:
                same = fh.read() == data
        except OSError:
            same = False
        if not same:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            tmp = dst + ".tmp"
            with open(tmp, "wb") as fh:
                fh.write(data)
            os.replace(tmp, dst)
        return dst
    except OSError:
        return src


# ---------------------------------------------------------------- COM plumbing
_COM = {}  # ctypes types and functions, made on first use (Windows only)


def _com():
    if _COM:
        return _COM
    import ctypes
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                    ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

    class PROPERTYKEY(ctypes.Structure):
        _fields_ = [("fmtid", GUID), ("pid", wintypes.DWORD)]

    class PROPVARIANT(ctypes.Structure):  # 16 bytes on 32-bit, 24 on 64-bit
        _fields_ = [("vt", wintypes.USHORT), ("r1", wintypes.USHORT), ("r2", wintypes.USHORT),
                    ("r3", wintypes.USHORT), ("ptr", ctypes.c_void_p), ("pad", ctypes.c_void_p)]

    def guid(text):
        g = GUID()
        ctypes.memmove(ctypes.byref(g), uuid.UUID(text).bytes_le, 16)
        return g

    fmtid = "{9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3}"  # PKEY_AppUserModel_*
    ole32 = ctypes.WinDLL("ole32")
    ole32.CoInitializeEx.restype = ctypes.c_long
    ole32.CoInitializeEx.argtypes = (ctypes.c_void_p, wintypes.DWORD)
    ole32.CoCreateInstance.restype = ctypes.c_long
    ole32.CoCreateInstance.argtypes = (ctypes.POINTER(GUID), ctypes.c_void_p, wintypes.DWORD,
                                       ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p))
    ole32.PropVariantClear.argtypes = (ctypes.POINTER(PROPVARIANT),)
    shell32 = ctypes.WinDLL("shell32")
    shell32.SHGetPropertyStoreForWindow.restype = ctypes.c_long
    shell32.SHGetPropertyStoreForWindow.argtypes = (wintypes.HWND, ctypes.POINTER(GUID),
                                                   ctypes.POINTER(ctypes.c_void_p))
    shell32.SHChangeNotify.restype = None
    shell32.SHChangeNotify.argtypes = (ctypes.c_long, wintypes.UINT, ctypes.c_void_p, ctypes.c_void_p)
    _COM.update(
        ctypes=ctypes, wintypes=wintypes, GUID=GUID, PROPERTYKEY=PROPERTYKEY,
        PROPVARIANT=PROPVARIANT, ole32=ole32, shell32=shell32,
        keys={name: PROPERTYKEY(guid(fmtid), pid) for name, pid in (
            ("RelaunchCommand", 2), ("RelaunchIconResource", 3),
            ("RelaunchDisplayNameResource", 4), ("ID", 5))},
        IID_IPropertyStore=guid("{886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99}"),
        CLSID_ShellLink=guid("{00021401-0000-0000-C000-000000000046}"),
        IID_IShellLinkW=guid("{000214F9-0000-0000-C000-000000000046}"),
        IID_IPersistFile=guid("{0000010b-0000-0000-C000-000000000046}"),
    )
    return _COM


def _call(obj, index: int, argtypes, *args) -> None:
    """Call method *index* of the COM object *obj*'s vtable; raises OSError on
    a failed HRESULT."""
    c = _com()
    ctypes = c["ctypes"]
    vtbl = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
    proto = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *argtypes)
    hr = proto(vtbl[index])(obj, *args)
    if hr < 0:
        raise OSError(f"COM call {index} failed: HRESULT {hr & 0xFFFFFFFF:#010x}")


def _release(obj) -> None:
    if obj:
        c = _com()
        ctypes = c["ctypes"]
        vtbl = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
        ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtbl[2])(obj)


def _query(obj, iid):
    c = _com()
    ctypes = c["ctypes"]
    out = ctypes.c_void_p()
    _call(obj, 0, (ctypes.POINTER(c["GUID"]), ctypes.POINTER(ctypes.c_void_p)),
          ctypes.byref(iid), ctypes.byref(out))
    return out


class _Apartment:
    """COM for the calling thread for the duration of a ``with`` block (the
    shortcut workers run off the UI thread, which Qt set up itself)."""

    def __enter__(self):
        hr = _com()["ole32"].CoInitializeEx(None, 0x2)  # COINIT_APARTMENTTHREADED
        self._owned = hr in (0, 1)  # S_OK, S_FALSE; RPC_E_CHANGED_MODE is not ours
        return self

    def __exit__(self, *exc):
        if self._owned:
            _com()["ole32"].CoUninitialize()


def _store_set(store, key: str, text: str) -> None:
    c = _com()
    ctypes = c["ctypes"]
    buf = ctypes.create_unicode_buffer(text)
    pv = c["PROPVARIANT"](31, 0, 0, 0, ctypes.cast(buf, ctypes.c_void_p), None)  # VT_LPWSTR
    _call(store, 6, (ctypes.POINTER(c["PROPERTYKEY"]), ctypes.POINTER(c["PROPVARIANT"])),
          ctypes.byref(c["keys"][key]), ctypes.byref(pv))


def _store_get(store, key: str) -> Optional[str]:
    c = _com()
    ctypes = c["ctypes"]
    pv = c["PROPVARIANT"]()
    _call(store, 5, (ctypes.POINTER(c["PROPERTYKEY"]), ctypes.POINTER(c["PROPVARIANT"])),
          ctypes.byref(c["keys"][key]), ctypes.byref(pv))
    try:
        return ctypes.wstring_at(pv.ptr) if pv.vt == 31 and pv.ptr else None
    finally:
        c["ole32"].PropVariantClear(ctypes.byref(pv))


def _window_store(hwnd: int):
    c = _com()
    ctypes = c["ctypes"]
    store = ctypes.c_void_p()
    hr = c["shell32"].SHGetPropertyStoreForWindow(hwnd, ctypes.byref(c["IID_IPropertyStore"]),
                                                  ctypes.byref(store))
    if hr < 0 or not store:
        raise OSError(f"no property store for window {hwnd:#x}")
    return store


# ------------------------------------------------------------------- windows
_PROP_MAX = 255  # characters a window's AppUserModel property holds


def short_path(path: str) -> str:
    """The 8.3 form of *path*, or *path* itself when it has none."""
    try:
        import ctypes

        buf = ctypes.create_unicode_buffer(32768)
        n = ctypes.windll.kernel32.GetShortPathNameW(path, buf, len(buf))
        return buf.value if 0 < n < len(buf) else path
    except Exception:
        return path


def command_line(program: str, args: str = "") -> str:
    """*program* and *args* as the command line of a pin: *program* in its 8.3
    form when the whole would not fit the 255 characters a pin keeps."""
    import subprocess

    tail = f" {args}" if args else ""
    line = subprocess.list2cmdline([program]) + tail
    if len(line) > _PROP_MAX:
        line = subprocess.list2cmdline([short_path(program)]) + tail
    return line


def set_window_identity(hwnd: int, command: str, icon: str, *, app_id: str = APP_ID,
                        name: str = APP_NAME) -> bool:
    """Give the top-level window *hwnd* TurboADB's app ID, and what a taskbar
    pin made of it shows and runs: *name*, *icon* (an ``.ico`` path) and
    *command* (a complete command line, see :func:`command_line`).  Without
    them a pin started the bare interpreter the window's process runs in.
    A command too long for a pin sets the app ID alone (returns False)."""
    if sys.platform != "win32" or not hwnd:
        return False
    icon_ref = f"{icon},0"
    if len(icon_ref) > _PROP_MAX:
        icon_ref = f"{short_path(icon)},0"
    try:
        with _Apartment():
            store = _window_store(hwnd)
            try:
                _store_set(store, "ID", app_id)
                if len(command) > _PROP_MAX:
                    return False
                _store_set(store, "RelaunchCommand", command)
                _store_set(store, "RelaunchDisplayNameResource", name)
                if len(icon_ref) <= _PROP_MAX:
                    _store_set(store, "RelaunchIconResource", icon_ref)
            finally:
                _release(store)
        return True
    except Exception:
        return False


def window_identity(hwnd: int) -> Optional[Dict[str, Optional[str]]]:
    """The AppUserModel properties of the window *hwnd* (None: unreadable)."""
    if sys.platform != "win32" or not hwnd:
        return None
    try:
        with _Apartment():
            store = _window_store(hwnd)
            try:
                return {key: _store_get(store, key) for key in _com()["keys"]}
            finally:
                _release(store)
    except Exception:
        return None


# ------------------------------------------------------------------ shortcuts
def _new_link():
    c = _com()
    ctypes = c["ctypes"]
    link = ctypes.c_void_p()
    hr = c["ole32"].CoCreateInstance(ctypes.byref(c["CLSID_ShellLink"]), None, 1,  # INPROC
                                     ctypes.byref(c["IID_IShellLinkW"]), ctypes.byref(link))
    if hr < 0 or not link:
        raise OSError(f"CoCreateInstance(ShellLink) failed: {hr & 0xFFFFFFFF:#010x}")
    return link


def _load(link, path: str, mode: int):
    c = _com()
    persist = _query(link, c["IID_IPersistFile"])
    try:
        _call(persist, 5, (c["wintypes"].LPCWSTR, c["wintypes"].DWORD), path, mode)  # Load
    except Exception:
        _release(persist)
        raise
    return persist


def read_shortcut(path: str) -> Optional[Dict[str, object]]:
    """``target``, ``args``, ``workdir``, ``icon`` (path, no index) and
    ``app_id`` of the shortcut *path*, or None when it can't be read."""
    if sys.platform != "win32" or not os.path.isfile(path):
        return None
    try:
        with _Apartment():
            c = _com()
            ctypes, wt = c["ctypes"], c["wintypes"]
            link = _new_link()
            try:
                persist = _load(link, path, 0)  # STGM_READ
                _release(persist)
                buf = ctypes.create_unicode_buffer(32768)
                out: Dict[str, object] = {}
                _call(link, 3, (wt.LPWSTR, ctypes.c_int, ctypes.c_void_p, wt.DWORD),
                      buf, len(buf), None, 0x4)  # GetPath, SLGP_RAWPATH
                out["target"] = buf.value
                _call(link, 10, (wt.LPWSTR, ctypes.c_int), buf, len(buf))  # GetArguments
                out["args"] = buf.value
                _call(link, 8, (wt.LPWSTR, ctypes.c_int), buf, len(buf))  # GetWorkingDirectory
                out["workdir"] = buf.value
                index = ctypes.c_int()
                _call(link, 16, (wt.LPWSTR, ctypes.c_int, ctypes.POINTER(ctypes.c_int)),
                      buf, len(buf), ctypes.byref(index))  # GetIconLocation
                out["icon"] = buf.value
                store = _query(link, c["IID_IPropertyStore"])
                try:
                    out["app_id"] = _store_get(store, "ID")
                finally:
                    _release(store)
                return out
            finally:
                _release(link)
    except Exception:
        return None


def write_shortcut(path: str, target: str, args: str, workdir: str, icon: str,
                   description: str, *, app_id: Optional[str] = APP_ID) -> bool:
    """Create or replace the shortcut *path*: *target* with *args* in
    *workdir*, showing *icon*, carrying TurboADB's *app_id* (None: none)."""
    if sys.platform != "win32":
        return False
    try:
        with _Apartment():
            c = _com()
            wt = c["wintypes"]
            link = _new_link()
            try:
                _call(link, 20, (wt.LPCWSTR,), target)  # SetPath
                _call(link, 11, (wt.LPCWSTR,), args or "")  # SetArguments
                _call(link, 9, (wt.LPCWSTR,), workdir or "")  # SetWorkingDirectory
                _call(link, 17, (wt.LPCWSTR, c["ctypes"].c_int), icon, 0)  # SetIconLocation
                _call(link, 7, (wt.LPCWSTR,), description)  # SetDescription
                if app_id:
                    store = _query(link, c["IID_IPropertyStore"])
                    try:
                        _store_set(store, "ID", app_id)
                        _call(store, 7, ())  # Commit
                    finally:
                        _release(store)
                persist = _query(link, c["IID_IPersistFile"])
                try:
                    tmp = path + ".tmp.lnk"
                    _call(persist, 6, (wt.LPCWSTR, wt.BOOL), tmp, True)  # Save
                finally:
                    _release(persist)
            finally:
                _release(link)
        os.replace(tmp, path)
        return True
    except Exception:
        try:
            os.remove(path + ".tmp.lnk")
        except OSError:
            pass
        return False


def set_shortcut_icon(path: str, icon: str) -> bool:
    """Point the shortcut *path* at *icon*, keeping everything else in it
    (its app ID above all: without it a pin stops grouping with the window)."""
    if sys.platform != "win32":
        return False
    try:
        with _Apartment():
            c = _com()
            wt = c["wintypes"]
            link = _new_link()
            try:
                persist = _load(link, path, 0x2)  # STGM_READWRITE
                try:
                    _call(link, 17, (wt.LPCWSTR, c["ctypes"].c_int), icon, 0)
                    _call(persist, 6, (wt.LPCWSTR, wt.BOOL), None, True)  # Save in place
                finally:
                    _release(persist)
            finally:
                _release(link)
        return True
    except Exception:
        return False


def pinned_shortcuts() -> List[str]:
    """The ``.lnk`` files of the apps pinned to this user's taskbar."""
    folder = os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Internet Explorer",
                          "Quick Launch", "User Pinned", "TaskBar")
    try:
        return [os.path.join(folder, n) for n in os.listdir(folder) if n.lower().endswith(".lnk")]
    except OSError:
        return []


def notify_changed(paths: Iterable[str]) -> None:
    """Tell Explorer and the taskbar that these shortcuts changed, and to drop
    the icons they had cached for them."""
    if sys.platform != "win32":
        return
    try:
        c = _com()
        shell32 = c["shell32"]
        for path in paths:
            buf = c["ctypes"].create_unicode_buffer(path)
            shell32.SHChangeNotify(0x00002000, 0x0005, buf, None)  # SHCNE_UPDATEITEM, SHCNF_PATHW
        shell32.SHChangeNotify(0x08000000, 0x3000, None, None)  # SHCNE_ASSOCCHANGED, FLUSHNOWAIT
    except Exception:
        pass


def is_turboadb_shortcut(info) -> bool:
    """Whether the shortcut *info* (of :func:`read_shortcut`) starts TurboADB:
    it has TurboADB's app ID, or starts a TurboADB program (one written by an
    earlier TurboADB, or pinned from one, has no app ID)."""
    if info.get("app_id") == APP_ID:
        return True
    target = ntpath.basename(str(info.get("target") or "")).lower()  # a Windows path everywhere
    if target in ("turboadb.exe", "turboadb-gui.exe"):
        return True
    if target.startswith("turboadb-") and target.endswith(".exe"):
        return True  # the one-file exe, TurboADB-3.0.0-win64.exe
    return target in ("pythonw.exe", "python.exe") and "turboadb" in str(info.get("args") or "")


def sync_shortcut_icons(icon: str, paths: Iterable[str]) -> List[str]:
    """Point every shortcut of *paths* that is TurboADB's at *icon*.  Returns
    the ones changed (Explorer and the taskbar are told)."""
    changed = []
    for path in paths:
        info = read_shortcut(path)
        if not info or not is_turboadb_shortcut(info):
            continue
        if os.path.normcase(str(info.get("icon") or "")) == os.path.normcase(icon):
            continue
        if set_shortcut_icon(path, icon):
            changed.append(path)
    if changed:
        notify_changed(changed)
    return changed
