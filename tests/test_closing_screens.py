"""Closing a tab, or TurboADB itself, while a screen is still closing or a
recording is still being saved.

- A closed tab was destroyed on the next turn of the event loop. The scrcpy
  window of an embedded screen is still inside the tab then: the stop worker
  closes it, and a scrcpy that has stopped answering is left attached until
  its process has been ended. Destroying the tab destroys that window with
  it, which waits for scrcpy: the whole of TurboADB froze until the worker
  had ended it, about six seconds later (sixteen for a screen that was
  recording). The tab is now only hidden, and destroyed once its screens
  have closed.
- Quitting while a recording was running, or still being saved, lost it: the
  app went on to stop the adb server under the pull of a device recording
  (the unfinished file was then removed), and a scrcpy still writing its MP4
  index got five seconds instead of its own fifteen to twenty. The window
  now stays, saying what it is saving, until the recording is on this PC.

Headless (offscreen Qt), except the one test of real windows: it runs the
screen panel in a process of its own on the Windows platform, with nothing
shown on screen. No adb, scrcpy or device is used: a stand-in process owns
the "scrcpy" window, and a stand-in handler "records".
"""

import os
import subprocess
import sys
import threading
import time

import pytest

pytest.importorskip("PyQt5")

from PyQt5 import sip  # noqa: E402
from PyQt5.QtCore import QEvent, QThread, pyqtSignal  # noqa: E402
from PyQt5.QtWidgets import QVBoxLayout, QWidget  # noqa: E402

import turboadb.gui.main_window as mw  # noqa: E402
import turboadb.gui.mirror_panel as mp  # noqa: E402


class _Tracker(QThread):
    result = pyqtSignal(list)

    def __init__(self, adb_path=None, parent=None):
        super().__init__(parent)

    def stop(self):
        pass

    def run(self):
        pass


class _Poll(QThread):
    result = pyqtSignal(list)
    failed = pyqtSignal(str)

    def __init__(self, adb_path=None, *, socket_only=False, parent=None):
        super().__init__(parent)

    def run(self):
        pass


class _Init(QThread):
    ready = pyqtSignal(bool, str)
    note = pyqtSignal(str)

    def __init__(self, adb_path=None, parent=None):
        super().__init__(parent)

    def run(self):
        pass


@pytest.fixture
def window(qapp, tmp_path, monkeypatch):
    """A MainWindow with inert workers, in a profile of the test's own."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setattr(mw, "_DeviceTracker", _Tracker)
    monkeypatch.setattr(mw, "_DevicesPoll", _Poll)
    monkeypatch.setattr(mw, "_AdbInitThread", _Init)
    for name in ("_check_tools", "_ensure_shortcuts", "_quiet_update_check"):
        monkeypatch.setattr(mw.MainWindow, name, lambda self: None)
    logged = []
    monkeypatch.setattr(mw.MainWindow, "_log", lambda self, text: logged.append(text))
    win = mw.MainWindow()
    win.logged = logged
    yield win
    win.close()
    win.deleteLater()
    _free_deleted(qapp)


def _free_deleted(qapp):
    qapp.processEvents()
    qapp.sendPostedEvents(None, QEvent.DeferredDelete)
    qapp.processEvents()


def _pump(qapp, until, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        _free_deleted(qapp)
        if until():
            return True
        time.sleep(0.01)
    _free_deleted(qapp)
    return until()


class _Signal:
    def __init__(self):
        self.slots = []

    def connect(self, slot):
        self.slots.append(slot)

    def emit(self, *args):
        for slot in list(self.slots):
            slot(*args)


class _Worker:
    """A launch or stop worker that reports when the test says so."""

    def start(self):
        pass

    def isRunning(self):
        return False

    def isFinished(self):  # (nothing to keep alive: never parked)
        return True


@pytest.fixture
def workers(monkeypatch):
    """The panel's scrcpy workers as stand-ins: ``launches``, ``stops`` (a
    screen closing) and ``record_closes`` (a scrcpy that recorded, closing)."""
    made = {"launches": [], "stops": [], "record_closes": []}

    class Launch(_Worker):
        def __init__(self, handler, opts, compat, log_path):
            self.opts, self.cancelled = opts, False
            self.done, self.fail = _Signal(), _Signal()
            made["launches"].append(self)

        def isRunning(self):  # scrcpy is being started until the test reports
            return True

        def cancel(self):
            self.cancelled = True

    def closer(kind):
        class Close(_Worker):
            def __init__(self, session, window_title=None, window_handle=None):
                self.session, self.done = session, _Signal()
                made[kind].append(self)

        return Close

    monkeypatch.setattr(mp, "_MirrorLaunchThread", Launch)
    monkeypatch.setattr(mp, "_MirrorStopThread", closer("stops"))
    monkeypatch.setattr(mp, "_RecWaitThread", closer("record_closes"))
    return made


class _Session:
    """A scrcpy session that runs until it is stopped."""

    log_path = None

    def __init__(self):
        self.running = True

    def read_log(self):
        return ""

    def stop(self, **_kwargs):
        self.running = False

    def wait(self, timeout=None):
        self.running = False
        return 0


class _Tab(QWidget):
    """A device tab as far as the window closes it: a screen panel inside,
    and close_session()."""

    def __init__(self, handler=None):
        super().__init__()
        if handler is None:
            handler = type("Handler", (), {"serial": "phone", "config": None})()
        self.panel = mp.MirrorPanel(handler, {"name": "phone"})
        QVBoxLayout(self).addWidget(self.panel)

    def show_screen(self):
        """An embedded screen is on: scrcpy's window is inside the panel."""
        self.session = _Session()
        self.panel._scrcpy, self.panel._embed_on = self.session, True
        self.panel._refresh_buttons()
        return self.session

    def close_session(self):
        self.panel.close_panel()


def _open(window, tab):
    window.tabs.addTab(tab, "phone")
    return tab


def test_a_closed_tab_is_destroyed_only_once_its_screen_has_closed(qapp, window, workers):
    tab = _open(window, _Tab())
    tab.show_screen()
    window._close_tab(0)
    assert window.tabs.count() == 0 and tab.isHidden()
    (closing,) = workers["stops"]  # the worker that closes scrcpy's window
    _free_deleted(qapp)
    assert not sip.isdeleted(tab)  # scrcpy's window may still be inside it
    closing.done.emit(True)
    assert _pump(qapp, lambda: sip.isdeleted(tab))


def test_a_tab_with_nothing_left_to_close_is_destroyed_at_once(qapp, window, workers):
    idle = _open(window, _Tab())
    plain = QWidget()  # a tab without a screen (the Webcam tab)
    plain.close_session = lambda: None
    window.tabs.addTab(plain, "Webcam")
    window._close_tab(1)
    window._close_tab(0)
    _free_deleted(qapp)
    assert sip.isdeleted(idle) and sip.isdeleted(plain)
    assert workers["stops"] == []


def test_a_tab_closed_mid_recording_waits_for_the_scrcpy_that_records(
    qapp, window, workers, tmp_path
):
    """scrcpy recording its own screen is closed by the recording's worker,
    which gives it fifteen seconds to finish the MP4."""
    tab = _open(window, _Tab())
    tab.show_screen()
    panel, logs = tab.panel, []
    panel.log.connect(logs.append)
    panel._recording, panel._rec_mode = True, "integrated"
    panel._rec_path = str(tmp_path / "clip.mp4")
    window._close_tab(0)
    (closing,) = workers["record_closes"]
    assert workers["stops"] == []
    _free_deleted(qapp)
    assert not sip.isdeleted(tab)
    (tmp_path / "clip.mp4").write_bytes(b"\x00\x00\x00\x20ftypisom")
    closing.done.emit(True)
    assert any(line.startswith("[OK] recording saved") for line in logs)  # still reported
    assert _pump(qapp, lambda: sip.isdeleted(tab))


def test_a_tab_closed_while_scrcpy_launches_still_closes_what_was_launched(
    qapp, window, workers
):
    """The launch worker may have handed over its session just before the
    close cancelled it: the panel is still there to stop it."""
    tab = _open(window, _Tab())
    tab.panel.start()
    (launch,) = workers["launches"]
    window._close_tab(0)
    _free_deleted(qapp)
    assert launch.cancelled and not sip.isdeleted(tab)
    session = _Session()
    launch.done.emit(session)  # handed over before the worker saw the cancel
    (closing,) = workers["stops"]
    assert closing.session is session
    _free_deleted(qapp)
    assert not sip.isdeleted(tab)
    closing.done.emit(True)
    assert _pump(qapp, lambda: sip.isdeleted(tab))


def test_a_device_tab_is_kept_until_the_screen_of_device_control_has_closed(
    qapp, window, workers
):
    from test_adb_processes import _Device, _connect, _tab

    tab = _tab(qapp)
    window.tabs.addTab(tab, "phone")
    _connect(tab, _Device())
    qapp.processEvents()
    mirror = tab.mirror_tab
    mirror._scrcpy, mirror._embed_on = _Session(), True
    window._close_tab(0)
    (closing,) = workers["stops"]
    _free_deleted(qapp)
    assert not sip.isdeleted(tab) and tab.isHidden()
    closing.done.emit(True)
    assert _pump(qapp, lambda: sip.isdeleted(tab))


def test_a_tab_whose_own_close_failed_still_has_its_screen_closed(qapp, window, workers):
    tab = _open(window, _Tab())
    session = tab.show_screen()
    tab.close_session = lambda: 1 / 0
    window._close_tab(0)
    assert any("could not close" in line for line in window.logged)
    (closing,) = workers["stops"]  # scrcpy is not left running in a hidden tab
    assert closing.session is session
    closing.done.emit(True)
    assert _pump(qapp, lambda: sip.isdeleted(tab))


# --------------------------------------------------------------------------- #
# quitting while a recording is still being saved
# --------------------------------------------------------------------------- #
_VIDEO = b"\x00\x00\x00\x20ftypisom" + b"video" * 100


class _Recorder:
    """A handler whose device records until it is stopped, then takes
    *pull_s* to hand the clip over (or until *release* is set)."""

    serial = "phone"
    config = None

    def __init__(self, pull_s=0.4, release=None, error=None):
        self.pull_s, self.release, self.error = pull_s, release, error
        self.recording = threading.Event()
        self.saved = threading.Event()

    def screen_record_continuous(self, path, *, stop_event=None, on_part=None, **_kwargs):
        self.recording.set()
        stop_event.wait(30)
        if self.release is not None:
            self.release.wait(30)
        else:
            time.sleep(self.pull_s)  # the pull
        if self.error:
            raise RuntimeError(self.error)
        with open(path, "wb") as fh:
            fh.write(_VIDEO)
        on_part(path)
        self.saved.set()
        return [path]


def _record_on_the_device(tab, path, monkeypatch):
    """Press Record on an idle screen: the device records (screenrecord)."""
    monkeypatch.setattr(
        mp.QFileDialog, "getSaveFileName", lambda *_a, **_k: (path, "MP4 video (*.mp4)")
    )
    tab.panel._begin_record()
    assert tab.panel._rec_mode == "device" and tab.panel._recording
    assert tab.panel.handler.recording.wait(5)


def _wait_for_workers(qapp, panel):
    assert _pump(qapp, lambda: not panel.recording_threads(), timeout=10)


def _when_the_saving_dialog_is_up(window, act):
    """Call ``act(dialog)`` from the event loop once the exit shows its dialog
    (the close itself waits inside ``window.close()``)."""
    from PyQt5.QtCore import QTimer

    give_up = time.monotonic() + 10

    def look():
        dialog = window.findChild(mp._SavingDialog)
        if dialog is not None and dialog.isVisible():
            act(dialog)
        elif time.monotonic() < give_up:
            QTimer.singleShot(20, look)

    QTimer.singleShot(20, look)


def test_quitting_waits_until_a_device_recording_is_on_this_pc(
    qapp, window, tmp_path, monkeypatch
):
    path = str(tmp_path / "drive.mp4")
    tab = _open(window, _Tab(_Recorder()))
    _record_on_the_device(tab, path, monkeypatch)
    shown = []
    monkeypatch.setattr(
        mp._SavingDialog, "showEvent", lambda self, event: shown.append(self.label.text())
    )
    window.close()  # File > Exit, without pressing Stop recording first
    assert os.path.getsize(path) > 0  # saved before the window went
    assert window.isHidden()
    assert len(shown) == 1 and path in shown[0]
    _wait_for_workers(qapp, tab.panel)


def test_close_without_saving_does_not_wait(qapp, window, tmp_path, monkeypatch):
    release = threading.Event()
    path = str(tmp_path / "drive.mp4")
    tab = _open(window, _Tab(_Recorder(release=release)))
    _record_on_the_device(tab, path, monkeypatch)
    try:
        _when_the_saving_dialog_is_up(window, lambda dialog: dialog.btn_skip.click())
        started = time.monotonic()
        window.close()
        assert time.monotonic() - started < 5 and window.isHidden()
        assert not os.path.exists(path)
        # asked once: closing again (the restart after an update does) goes straight on
        asked = []
        monkeypatch.setattr(mp._SavingDialog, "exec_", lambda self: asked.append(self))
        window.close()
        assert asked == []
    finally:
        release.set()
    assert tab.panel.handler.saved.wait(10)


def test_escape_does_not_throw_the_recording_away(qapp, window, tmp_path, monkeypatch):
    from PyQt5.QtCore import Qt
    from PyQt5.QtTest import QTest

    path = str(tmp_path / "drive.mp4")
    tab = _open(window, _Tab(_Recorder(pull_s=1.0)))
    _record_on_the_device(tab, path, monkeypatch)
    focused = []

    def press_escape(dialog):
        focused.append(dialog.focusWidget() is dialog.btn_skip)
        QTest.keyClick(dialog, Qt.Key_Escape)
        dialog.close()  # (and Alt+F4)

    _when_the_saving_dialog_is_up(window, press_escape)
    window.close()
    assert os.path.getsize(path) > 0  # still waited for
    assert focused == [False]  # nor is its button a stray Space away
    _wait_for_workers(qapp, tab.panel)


def test_quitting_gives_a_scrcpy_recording_its_own_time_to_finish(qapp, window, tmp_path):
    path = str(tmp_path / "screen.mp4")

    class Recorder(_Session):
        """A windowless scrcpy beside the live screen: stopping it takes a
        moment, in which it writes the MP4 index."""

        def __init__(self):
            super().__init__()
            self.stop_calls = []

        def stop(self, **kwargs):
            self.stop_calls.append(kwargs)
            time.sleep(0.4)
            with open(path, "wb") as fh:
                fh.write(_VIDEO)
            self.running = False

    tab = _open(window, _Tab())
    panel = tab.panel
    recorder = Recorder()
    panel._recording, panel._rec_mode = True, "parallel"
    panel._rec_path, panel._rec_session = path, recorder
    window.close()
    assert os.path.getsize(path) > 0
    assert recorder.stop_calls == [{"timeout": mp._RecorderStopThread.CLOSE_TIMEOUT}]
    _wait_for_workers(qapp, panel)


def test_quitting_says_so_when_the_recording_could_not_be_saved(
    qapp, window, tmp_path, monkeypatch
):
    path = str(tmp_path / "drive.mp4")
    tab = _open(window, _Tab(_Recorder(error="adb pull failed (exit 1): device offline")))
    _record_on_the_device(tab, path, monkeypatch)
    warned = []
    monkeypatch.setattr(mp.QMessageBox, "warning", lambda *args, **_k: warned.append(args[2]))
    window.close()
    assert len(warned) == 1 and path in warned[0] and "not saved" in warned[0]
    assert window.isHidden()
    _wait_for_workers(qapp, tab.panel)


def test_quitting_waits_for_the_recording_of_a_tab_closed_just_before(
    qapp, window, tmp_path, monkeypatch
):
    path = str(tmp_path / "drive.mp4")
    tab = _open(window, _Tab(_Recorder(pull_s=1.0)))
    _record_on_the_device(tab, path, monkeypatch)
    window._close_tab(0)  # the tab first: its recording is being pulled
    _free_deleted(qapp)
    assert not sip.isdeleted(tab)  # kept until the recording is saved
    window.close()
    assert os.path.getsize(path) > 0
    assert _pump(qapp, lambda: sip.isdeleted(tab))


def test_quitting_stops_and_saves_a_recording_whose_tab_failed_to_close(
    qapp, window, tmp_path, monkeypatch
):
    """A tab whose own close fails leaves its recording running: the wait
    would never end, as nothing had told the recorder to stop."""
    path = str(tmp_path / "drive.mp4")
    tab = _open(window, _Tab(_Recorder()))
    _record_on_the_device(tab, path, monkeypatch)
    tab.close_session = lambda: 1 / 0
    started = time.monotonic()
    window.close()
    assert time.monotonic() - started < 10
    assert os.path.getsize(path) > 0 and window.isHidden()
    _wait_for_workers(qapp, tab.panel)


def test_a_minimized_window_comes_back_to_show_what_it_waits_for(
    qapp, window, tmp_path, monkeypatch
):
    path = str(tmp_path / "drive.mp4")
    tab = _open(window, _Tab(_Recorder()))
    _record_on_the_device(tab, path, monkeypatch)
    window.showMinimized()
    assert window.isMinimized()
    minimized = []
    _when_the_saving_dialog_is_up(window, lambda dialog: minimized.append(window.isMinimized()))
    window.close()  # from the taskbar
    assert minimized == [False] and os.path.getsize(path) > 0
    _wait_for_workers(qapp, tab.panel)


def test_quitting_with_nothing_being_saved_asks_nothing(qapp, window, monkeypatch):
    _open(window, _Tab())
    asked = []
    monkeypatch.setattr(mp._SavingDialog, "exec_", lambda self: asked.append(self))
    assert mp.finish_recordings(window) is True
    window.close()
    assert window.isHidden() and asked == []


# --------------------------------------------------------------------------- #
# with real windows: a hung scrcpy inside the closed tab
# --------------------------------------------------------------------------- #
# Run in a process of its own on the Windows platform; nothing is shown on
# screen. Prints the longest time the event loop stood still, and whether
# the tab was destroyed in the end.
_REAL_WINDOWS = r'''
import os, subprocess, sys, time
from PyQt5 import sip
from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtWidgets import QApplication, QTabWidget, QVBoxLayout, QWidget
sys.path.insert(0, os.path.join(os.getcwd(), "tests"))
import turboadb.gui.mirror_panel as mp
from test_hung_scrcpy_window import _StandIn

STOP_S = 4.0  # how long the stop worker waits before it ends the process


class Session:
    """The scrcpy session of the stand-in window's process."""

    log_path = None

    def __init__(self, stand_in):
        self.proc = stand_in.proc

    @property
    def running(self):
        return self.proc.poll() is None

    def read_log(self):
        return ""

    def wait(self, timeout=None):
        return self.proc.wait(timeout)

    def stop(self, timeout=5.0):
        try:
            self.proc.wait(STOP_S)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(5)


app = QApplication([])
top = QWidget()
top.setAttribute(Qt.WA_DontShowOnScreen, True)
tabs = QTabWidget()
QVBoxLayout(top).addWidget(tabs)
page = QWidget()
panel = mp.MirrorPanel(type("Handler", (), {"serial": "s", "config": None})(), {"name": "s"})
QVBoxLayout(page).addWidget(panel)
tabs.addTab(page, "phone")
top.resize(900, 700)
top.show()
app.processEvents()

stand_in = _StandIn()
if not stand_in.hwnd or app.platformName() != "windows":
    print("no windows here", flush=True)
    stand_in.quit()
    sys.exit(0)
container = int(panel.container.winId())
mp._reparent(stand_in.hwnd, container, panel.container.width(), panel.container.height())
panel._child_hwnd, panel._embed_parent_hwnd = stand_in.hwnd, container
panel._scrcpy, panel._embed_on = Session(stand_in), True
panel._refresh_buttons()
app.processEvents()
stand_in.hang(20)  # until it is ended

beats = [time.monotonic()]
longest = [0.0]


def beat():
    now = time.monotonic()
    longest[0] = max(longest[0], now - beats[0])
    beats[0] = now


heart = QTimer()
heart.timeout.connect(beat)
heart.start(20)


def close_tab():  # as MainWindow._close_tab does
    panel.close_panel()
    tabs.removeTab(0)
    mp.delete_when_released(page)


def report():
    if not sip.isdeleted(page) and time.monotonic() < give_up:
        QTimer.singleShot(50, report)
        return
    print("longest stall: %.3f" % longest[0], flush=True)
    print("tab destroyed:", sip.isdeleted(page), flush=True)
    if stand_in.proc.poll() is None:
        stand_in.proc.kill()
    top.close()
    app.quit()


give_up = time.monotonic() + 20
QTimer.singleShot(100, close_tab)
QTimer.singleShot(300, report)
app.exec_()
'''


@pytest.mark.skipif(os.name != "nt", reason="Win32 windows of another process")
def test_closing_a_tab_never_waits_for_a_hung_embedded_scrcpy(tmp_path):
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = dict(os.environ, QT_QPA_PLATFORM="windows", PYTHONPATH=root, HOME=str(tmp_path),
               USERPROFILE=str(tmp_path), TURBOADB_AUTO_FETCH="0", TURBOADB_NO_LAUNCHER="1")
    done = subprocess.run([sys.executable, "-c", _REAL_WINDOWS], cwd=root, env=env,
                          capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, (done.returncode, done.stdout, done.stderr)
    if "no windows here" in done.stdout:
        pytest.skip("no window could be created in this session")
    lines = dict(line.split(": ", 1) for line in done.stdout.splitlines() if ": " in line)
    # the stand-in hangs until the stop worker ends it, five seconds on: a tab
    # destroyed at once stood still that long (a busy test machine does not)
    assert float(lines["longest stall"]) < 2.5, done.stdout
    assert lines["tab destroyed"] == "True", done.stdout
