"""Opening a terminal or a device never waits on Windows starting a program (6.3.0).

Every test here is about work that used to hold the window's thread: the
Android terminal started ``adb shell`` on it, PowerShell and CMD started
their shell on it, and a device that connected built Device Control from the
bottom up, so the stylesheet was applied to all of it again at every step.
Starting a program took up to a fifth of a second while others started too
(a device connecting, more terminals opening).
"""

import threading
import time

import pytest

pytest.importorskip("PyQt5")

from PyQt5.QtCore import QEvent, QObject  # noqa: E402

from test_android_shell_pty import _Handler, _close, _enter, _pump, _settle  # noqa: E402


class _SlowStart(_Handler):
    """A handler whose ``open_shell`` waits until the test lets it go, the
    way adb.exe can take its time to start."""

    def __init__(self):
        super().__init__()
        self.go = threading.Event()
        self.threads = []

    def open_shell(self, tty=True, safe=None):
        self.threads.append(threading.current_thread())
        self.go.wait(5)
        return super().open_shell(tty=tty, safe=safe)


def _android(handler):
    from turboadb.gui.device_tab import _AndroidShellWidget

    widget = _AndroidShellWidget(handler, device_name="mock", info={"kind": "phone"},
                                 autostart=False)
    widget.resize(900, 500)
    return widget


# --------------------------------------------------------------------------- #
# the Android terminal
# --------------------------------------------------------------------------- #
def test_the_android_terminal_starts_adb_off_the_windows_thread(qapp):
    """Issue: the terminal started adb shell on the window's thread: a
    fifth of a second without an answer each time a device connected or a
    terminal opened."""
    handler = _SlowStart()
    widget = _android(handler)
    try:
        began = time.monotonic()
        widget.ensure_started()
        # a start on this thread would wait the whole 5 s for adb (a slow CI
        # runner may take a moment to draw the banner)
        assert time.monotonic() - began < 3.0  # adb has not even started
        assert _pump(qapp, lambda: handler.threads)
        assert handler.threads[0] is not threading.main_thread()
        assert widget.term._alive  # ready to take what is typed

        _enter(widget.term, "ls")  # typed while adb starts
        handler.go.set()
        _settle(qapp, widget, "acct\ncache\nPD2318:/ $ ")
        assert handler.opened == [True]
    finally:
        handler.go.set()
        _close(widget)


def test_a_terminal_closed_while_adb_starts_leaves_no_shell_behind(qapp):
    handler = _SlowStart()
    widget = _android(handler)
    try:
        widget.ensure_started()
        assert _pump(qapp, lambda: handler.threads)
        widget.close_panel()
        handler.go.set()
        assert _pump(qapp, lambda: handler.sessions)
        shell = handler.sessions[0]
        assert _pump(qapp, lambda: not shell.running)  # closed as soon as it opened
    finally:
        handler.go.set()
        widget.deleteLater()


def test_restarting_while_adb_starts_keeps_only_the_new_shell(qapp):
    handler = _SlowStart()
    widget = _android(handler)
    try:
        widget.ensure_started()
        assert _pump(qapp, lambda: len(handler.threads) == 1)
        widget.restart_shell()
        assert _pump(qapp, lambda: len(handler.threads) == 2)
        handler.go.set()
        assert _pump(qapp, lambda: len(handler.sessions) == 2)  # (in either order)
        # the first is closed as soon as it opened; the terminal has the second
        assert _pump(qapp, lambda: [s.running for s in handler.sessions].count(True) == 1)
        _settle(qapp, widget, "PD2318:/ $ ")
        kept = next(s for s in handler.sessions if s.running)
        assert widget.session._session is kept and widget.session.running
    finally:
        handler.go.set()
        _close(widget)


# --------------------------------------------------------------------------- #
# PowerShell and CMD
# --------------------------------------------------------------------------- #
class _Stdin:
    def __init__(self):
        self.data = b""

    def write(self, view):
        self.data += bytes(view)
        return len(view)

    def flush(self):
        pass


class _Proc:
    pid = 4242
    stdout = None

    def __init__(self):
        self.stdin = _Stdin()

    def poll(self):
        return None


def _slow_shells(monkeypatch, error=None):
    """LocalShellSession with a ``Popen`` that waits for the test (or fails
    with *error*); returns the module, the gate and the record of calls."""
    import turboadb.tools as tools
    from turboadb.gui import local_terminal as lt

    go = threading.Event()
    calls = []

    def popen(argv, **kwargs):
        calls.append(threading.current_thread())
        go.wait(5)
        if error is not None:
            raise error
        proc = _Proc()
        calls.append(proc)
        return proc

    monkeypatch.setattr(lt.subprocess, "Popen", popen)
    monkeypatch.setattr(lt, "find_adb", lambda *_a, **_k: None)
    monkeypatch.setattr(tools, "find_scrcpy", lambda *_a, **_k: None)
    monkeypatch.setattr(lt, "_get_registry_env", lambda: {})
    return lt, go, calls


def test_a_local_shell_starts_off_the_windows_thread(monkeypatch):
    """Issue: PowerShell and CMD were started on the window's thread."""
    lt, go, calls = _slow_shells(monkeypatch)
    try:
        began = time.monotonic()
        session = lt.LocalShellSession("cmd", adb_path="__missing__")
        assert time.monotonic() - began < 3.0  # the shell has not started yet (5 s if it had)
        assert session.running and session.read() == b""
        assert session.send(b"dir\r\n") and session.send("cd ..\r\n")  # typed meanwhile
        deadline = time.monotonic() + 5
        while not calls and time.monotonic() < deadline:
            time.sleep(0.01)
        assert calls and calls[0] is not threading.main_thread()
        go.set()
        assert session.wait_started(5)
        proc = calls[1]
        deadline = time.monotonic() + 5
        while proc.stdin.data != b"dir\ncd ..\n" and time.monotonic() < deadline:
            time.sleep(0.01)
        assert proc.stdin.data == b"dir\ncd ..\n"  # in order, once it ran
        assert session.proc is proc and session.running
    finally:
        go.set()


def test_a_local_shell_closed_while_it_starts_is_ended_once_it_runs(monkeypatch):
    lt, go, calls = _slow_shells(monkeypatch)
    ended = []
    monkeypatch.setattr(lt, "_kill_process_tree", lambda proc, wait_s=2.0: ended.append(proc))
    session = lt.LocalShellSession("powershell", adb_path="__missing__")
    session.close()  # returns at once
    assert not session.running and not session.send(b"dir\n")
    assert not ended  # nothing to end yet
    go.set()
    assert lt.join_closers(5) == 0
    assert ended == [calls[1]]


def test_a_local_shell_that_cannot_start_says_why(qapp, monkeypatch):
    lt, go, _calls = _slow_shells(monkeypatch, error=OSError("cmd.exe is missing"))
    go.set()
    session = lt.LocalShellSession("cmd", adb_path="__missing__")
    assert session.wait_started(5)
    assert not session.running and isinstance(session.start_error, OSError)
    assert session.send(b"dir\n") is False and session.proc is None

    from turboadb.gui.device_tab import _LocalShellWidget

    widget = _LocalShellWidget("cmd", serial="V2318")
    try:
        widget.ensure_started()
        assert _pump(qapp, lambda: "[Could not start local cmd: cmd.exe is missing]"
                     in widget.term.toPlainText())
        assert not widget.term._alive
    finally:
        widget.close_panel()
        widget.deleteLater()


# --------------------------------------------------------------------------- #
# Device Control is built in its place
# --------------------------------------------------------------------------- #
class _Moves(QObject):
    def __init__(self, widget):
        super().__init__(widget)
        self.count = 0
        widget.installEventFilter(self)

    def eventFilter(self, _watched, event):
        if event.type() == QEvent.ParentChange:
            self.count += 1
        return False


def test_device_control_is_built_where_it_goes(qapp, monkeypatch):
    """Issue: Device Control was built away from the window and then moved
    into a card, a splitter, its page and the device tab: each move applied
    the stylesheet again to all of its widgets, which took longer than
    building them."""
    from turboadb.gui import device_tab as dt
    from test_adb_processes import _Device, _close as close_tab, _connect, _tab

    seen = {}

    class Mirror(dt.MirrorPanel):
        def __init__(self, *args, parent=None, **kwargs):
            super().__init__(*args, parent=parent, **kwargs)
            seen["mirror"] = (parent, _Moves(self))

    class Controls(dt.ControlsPanel):
        def __init__(self, *args, parent=None, **kwargs):
            super().__init__(*args, parent=parent, **kwargs)
            seen["controls"] = (parent, _Moves(self))

    monkeypatch.setattr(dt, "MirrorPanel", Mirror)
    monkeypatch.setattr(dt, "ControlsPanel", Controls)
    tab = _tab(qapp)
    try:
        tab.show()
        _connect(tab, _Device())
        qapp.processEvents()
        view = tab._control_view
        assert tab.inner.indexOf(view) >= 0
        names = [tab.inner.tabText(i) for i in range(tab.inner.count())]
        assert names[:4] == ["Terminal", "Logcat", "Files", "Device Control"]
        mirror_parent, mirror_moves = seen["mirror"]
        controls_parent, controls_moves = seen["controls"]
        assert mirror_parent is view.screen_card and controls_parent is view.controls_card
        assert mirror_moves.count == 0 and controls_moves.count == 0  # never moved
        assert tab.mirror_tab.window() is tab.window()
        assert view.mirror is tab.mirror_tab and view.controls is tab.controls
    finally:
        close_tab(qapp, tab)
