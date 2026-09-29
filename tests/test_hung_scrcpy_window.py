"""A scrcpy whose window stops answering never freezes TurboADB.

Moving, sizing or re-parenting scrcpy's embedded window, and moving the
keyboard to or from it, send the window a message and wait for its answer. A
scrcpy whose window thread hangs (a device disconnecting, a stuck decoder or
GPU driver) never answers, so every one of those calls hung TurboADB's UI with
it. Now the window is asked first, with a bounded message, and left alone
while it does not answer.

The stand-in for scrcpy is a real window in another process whose thread
stops handling messages on request. Windows only.
"""

import ctypes
import os
import subprocess
import sys
import time

import pytest

pytest.importorskip("PyQt5")
pytestmark = pytest.mark.skipif(os.name != "nt", reason="Win32 windows of another process")

# A hidden window in its own process; "hang <seconds>" on stdin stops its
# thread from handling messages for that long, "quit" ends it.
_STAND_IN = r"""
import ctypes, sys, threading, time
from ctypes import wintypes
u = ctypes.WinDLL("user32")
k = ctypes.WinDLL("kernel32")
LRESULT = ctypes.c_ssize_t
PROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
u.DefWindowProcW.restype = LRESULT
u.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
u.CreateWindowExW.restype = wintypes.HWND
u.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND, wintypes.HMENU,
    wintypes.HINSTANCE, wintypes.LPVOID]
u.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT,
    wintypes.UINT, wintypes.UINT]
u.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
u.DispatchMessageW.restype = LRESULT
k.GetModuleHandleW.restype = wintypes.HMODULE
k.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", PROC), ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON), ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH), ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR)]
proc = PROC(lambda h, m, w, l: u.DefWindowProcW(h, m, w, l))
wc = WNDCLASSW()
wc.lpfnWndProc = proc
wc.hInstance = k.GetModuleHandleW(None)
wc.lpszClassName = "TurboAdbScrcpyStandIn"
u.RegisterClassW(ctypes.byref(wc))
hwnd = u.CreateWindowExW(0, wc.lpszClassName, "scrcpy stand-in", 0x80000000,
    -32000, -32000, 320, 240, None, None, wc.hInstance, None)
print(int(hwnd or 0), flush=True)
orders = []
def read():
    for line in sys.stdin:
        orders.append(line.strip())
    orders.append("quit")
threading.Thread(target=read, daemon=True).start()
msg = wintypes.MSG()
while True:
    while u.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
        u.DispatchMessageW(ctypes.byref(msg))
    if orders:
        order = orders.pop(0)
        if order == "quit":
            break
        if order.startswith("hang"):
            print("hanging", flush=True)
            time.sleep(float(order.split()[1]))
            print("awake", flush=True)
    time.sleep(0.005)
"""

# How long the stand-in hangs: far longer than any wait these tests accept.
_HANG_S = 3.0
_PROMPT_S = 1.0


class _StandIn:
    def __init__(self):
        self.proc = subprocess.Popen(
            [sys.executable, "-c", _STAND_IN],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self.hwnd = int(self.proc.stdout.readline() or 0)

    def hang(self, seconds=_HANG_S):
        self.proc.stdin.write(f"hang {seconds}\n")
        self.proc.stdin.flush()
        assert self.proc.stdout.readline().strip() == "hanging"
        time.sleep(0.1)

    def wait_awake(self):
        assert self.proc.stdout.readline().strip() == "awake"

    def quit(self):
        if self.proc.poll() is None:
            try:
                self.proc.stdin.write("quit\n")
                self.proc.stdin.flush()
                self.proc.wait(10)
            except (OSError, subprocess.TimeoutExpired):
                self.proc.kill()
                self.proc.wait(5)


@pytest.fixture
def mp():
    import turboadb.gui.mirror_panel as mirror_panel

    mirror_panel._silent_until.clear()
    yield mirror_panel
    mirror_panel._silent_until.clear()


@pytest.fixture
def stand_in():
    made = []

    def start():
        window = _StandIn()
        made.append(window)
        if not window.hwnd:
            pytest.skip("no window could be created in this session")
        return window

    yield start
    for window in made:
        window.quit()


@pytest.fixture
def parent_window():
    """A hidden top-level window of this thread, as the embed container is."""
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32")
    kernel32 = ctypes.WinDLL("kernel32")
    lresult = ctypes.c_ssize_t
    proc_type = ctypes.WINFUNCTYPE(
        lresult, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
    )
    user32.DefWindowProcW.restype = lresult
    user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.CreateWindowExW.argtypes = [
        wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
    ]
    user32.DestroyWindow.argtypes = [wintypes.HWND]
    user32.UnregisterClassW.argtypes = [wintypes.LPCWSTR, wintypes.HINSTANCE]
    kernel32.GetModuleHandleW.restype = wintypes.HMODULE
    kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]

    class WndClass(ctypes.Structure):
        _fields_ = [
            ("style", wintypes.UINT), ("lpfnWndProc", proc_type), ("cbClsExtra", ctypes.c_int),
            ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE),
            ("hIcon", wintypes.HICON), ("hCursor", wintypes.HANDLE),
            ("hbrBackground", wintypes.HBRUSH), ("lpszMenuName", wintypes.LPCWSTR),
            ("lpszClassName", wintypes.LPCWSTR),
        ]

    # The class lives only as long as its window procedure: a class name that
    # outlived an earlier test would call that test's freed callback.
    window_proc = proc_type(lambda h, m, w, l: user32.DefWindowProcW(h, m, w, l))
    wc = WndClass()
    wc.lpfnWndProc = window_proc
    wc.hInstance = kernel32.GetModuleHandleW(None)
    wc.lpszClassName = f"TurboAdbEmbedParentForTests{time.monotonic_ns()}"
    if not user32.RegisterClassW(ctypes.byref(wc)):
        pytest.skip("no window class could be registered in this session")
    hwnd = user32.CreateWindowExW(
        0, wc.lpszClassName, "embed parent", 0x80000000, -32000, -32000, 640, 480,
        None, None, wc.hInstance, None,
    )
    try:
        if not hwnd:
            pytest.skip("no window could be created in this session")
        yield int(hwnd)
    finally:
        if hwnd:
            user32.DestroyWindow(hwnd)
        user32.UnregisterClassW(wc.lpszClassName, wc.hInstance)


def _size(mp, hwnd):
    from ctypes import wintypes

    _c, u = mp._win_api()
    rect = wintypes.RECT()
    u.GetWindowRect(hwnd, ctypes.byref(rect))
    return rect.right - rect.left, rect.bottom - rect.top


def _timed(fn):
    start = time.monotonic()
    result = fn()
    return result, time.monotonic() - start


def test_a_window_that_stops_answering_is_noticed_without_waiting_for_it(mp, stand_in):
    window = stand_in()
    assert mp._answering(window.hwnd)
    window.hang()
    answered, waited = _timed(lambda: mp._answering(window.hwnd))
    assert not answered and waited < _PROMPT_S
    # asked again only after a pause: a hung scrcpy costs one short wait a second
    answered, waited = _timed(lambda: mp._answering(window.hwnd))
    assert not answered and waited < 0.05
    window.wait_awake()
    time.sleep(mp._SILENT_RETRY_S)
    assert mp._answering(window.hwnd)
    window.quit()
    assert mp._answering(window.hwnd)  # gone: nothing would wait on it


def test_resizing_the_screen_never_waits_for_a_hung_scrcpy(qapp, mp, stand_in):
    window = stand_in()
    panel = mp.MirrorPanel(type("H", (), {"serial": "s", "config": None})(), {"name": "s"})
    try:
        panel.container.resize(400, 300)
        dpr = panel.devicePixelRatioF() or 1.0
        wanted = (round(400 * dpr), round(300 * dpr))
        panel._child_hwnd = window.hwnd
        window.hang()
        _result, waited = _timed(panel._fit)
        assert waited < _PROMPT_S
        assert _size(mp, window.hwnd) == (320, 240)  # left alone while it hangs
        window.wait_awake()
        time.sleep(mp._SILENT_RETRY_S)
        panel._fit()  # the next fit, once it answers again
        assert _size(mp, window.hwnd) == wanted
    finally:
        panel._child_hwnd = None
        panel.close_panel()
        panel.close()


def test_stopping_never_waits_for_a_hung_embedded_scrcpy(mp, stand_in, parent_window):
    window = stand_in()
    mp._reparent(window.hwnd, parent_window, 300, 200)
    _c, u = mp._win_api()
    getter = getattr(u, "GetWindowLongPtrW", None) or u.GetWindowLongW
    window.hang()
    closed, waited = _timed(lambda: mp._post_close(None, window.hwnd))
    # not touched: the stop ends the process instead of waiting for it
    assert closed is False and waited < mp._CLOSE_ANSWER_TIMEOUT_MS / 1000.0 + _PROMPT_S
    assert getter(window.hwnd, mp._GWL_STYLE) & mp._WS_CHILD
    assert int(u.GetAncestor(window.hwnd, mp._GA_PARENT) or 0) == parent_window


def test_the_keyboard_is_never_moved_by_waiting_for_a_hung_scrcpy(mp, stand_in, parent_window):
    window = stand_in()
    mp._reparent(window.hwnd, parent_window, 300, 200)
    if not mp._focus_embedded_child(window.hwnd, parent_window):
        pytest.skip("Windows did not give the embedded window the keyboard here")
    window.hang()
    moved, waited = _timed(lambda: mp._release_child_focus(window.hwnd, parent_window))
    assert moved is False and waited < _PROMPT_S
    moved, waited = _timed(lambda: mp._claim_native_focus(parent_window))
    assert moved is False and waited < _PROMPT_S
    window.wait_awake()
    time.sleep(mp._SILENT_RETRY_S)
    assert mp._release_child_focus(window.hwnd, parent_window)  # once it answers


def test_the_window_calls_declare_their_handle_arguments(mp):
    """Without argument types ctypes passes a window handle as a C int, which a
    64-bit handle value does not fit."""
    from ctypes import wintypes

    _c, u = mp._win_api()
    for name in ("IsWindow", "IsWindowVisible", "GetClientRect", "GetWindowRect",
                 "SendMessageTimeoutW", "MoveWindow"):
        assert getattr(u, name).argtypes[0] is wintypes.HWND, name
