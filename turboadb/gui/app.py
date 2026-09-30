"""GUI entry point: builds the QApplication, installs a crash-safe exception
hook (popup + log, non-fatal), runs first-run tasks, applies the saved theme at
the app level, and shows the main window."""

from __future__ import annotations

import logging
import os
import sys
import traceback

from PyQt5.QtCore import QLockFile, QObject, pyqtSignal, pyqtSlot
from PyQt5.QtGui import QIcon
from PyQt5.QtWidgets import QApplication, QMessageBox

from ..config import user_path
from .main_window import MainWindow, ICON_PATH

_window = None  # set after creation, used by the exception hook
_instance_lock = None  # kept alive for the lifetime of the GUI process
_instance_mutex = None  # Windows' atomic duplicate-launch guard


def _flag_dir() -> str:
    """``~/.turboadb``, resolved on every call like every other user file
    (see :func:`turboadb.config.user_dir`): frozen at import, the crash log,
    the log sweep and the instance lock kept using the profile the module
    was first imported under."""
    return user_path()


def _crash_log(text: str):
    """Append *text* to crash.log. Plain file I/O: safe from any thread."""
    try:
        folder = _flag_dir()
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, "crash.log"), "a", encoding="utf-8") as fh:
            fh.write(text + "\n")
    except Exception:
        pass


def _install_app_logging():
    """A rotating debug log at ~/.turboadb/turboadb.log capturing the library's
    log records (real adb commands, durations, errors) and any background-thread
    crash — so "it didn't work" reports have something concrete to attach.
    Best-effort; never fatal if logging can't be set up."""
    import logging
    import logging.handlers
    import threading

    try:
        folder = _flag_dir()
        os.makedirs(folder, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            os.path.join(folder, "turboadb.log"),
            maxBytes=1_000_000,
            backupCount=2,
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
        root = logging.getLogger("turboadb")
        root.setLevel(logging.DEBUG)
        # avoid stacking duplicate handlers if main() is ever re-entered
        if not any(isinstance(h, logging.handlers.RotatingFileHandler) for h in root.handlers):
            root.addHandler(handler)
    except Exception:
        pass
    # An exception in a Python thread (threading.Thread) never reaches
    # sys.excepthook, so it is written to crash.log here. (One escaping a
    # QThread's run() does reach sys.excepthook, on that worker thread: see
    # _install_excepthook.)
    try:

        def _thread_hook(args):
            import traceback

            _crash_log(
                "[background thread] "
                + "".join(
                    traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)
                )
            )

        threading.excepthook = _thread_hook
    except Exception:
        pass


_error_box = None  # the one visible "unexpected error" popup, if any
_suppressed_errors = 0  # errors raised while that popup was already open
_error_relay = None  # takes uncaught errors to the GUI thread (see _ErrorRelay)


def _on_error_box_closed(*_):
    global _error_box, _suppressed_errors
    _error_box = None
    _suppressed_errors = 0


def _show_error_popup(exc_type, exc) -> None:
    """Show ONE non-modal error popup; further errors update it instead of
    stacking a new modal box per exception (a repeating timer error used to
    bury the window under dialogs). GUI thread only, like every widget."""
    global _error_box, _suppressed_errors
    from PyQt5.QtCore import Qt

    box = _error_box
    if box is not None:
        try:
            if box.isVisible():
                _suppressed_errors += 1
                box.setInformativeText(
                    f"{_suppressed_errors} more error(s) since — details are in the log."
                )
                return
        except RuntimeError:  # deleted on close
            pass
        _error_box = None
    box = QMessageBox(
        QMessageBox.Warning,
        "TurboADB — error",
        f"{exc_type.__name__}: {exc}\n\nThe app stayed open; details are in the log.",
        QMessageBox.Ok,
        _window,
    )
    box.setAttribute(Qt.WA_DeleteOnClose, True)
    box.setWindowModality(Qt.NonModal)
    box.finished.connect(_on_error_box_closed)
    _error_box = box
    _suppressed_errors = 0
    box.show()


def _report_error(exc_type, text: str, details: str) -> None:
    """Show an uncaught error in the log panel and in the one error popup.
    GUI thread only: :class:`_ErrorRelay` brings every error here."""
    if _window is not None:
        try:
            _window.log_panel.append(f"[ERROR] Unexpected error:\n{details.rstrip()}")
        except Exception:
            pass
    try:
        _show_error_popup(exc_type, text)
    except Exception as popup_exc:  # never recurse into the hook
        _crash_log(f"[excepthook] could not show the error popup: {popup_exc!r}")


class _ErrorRelay(QObject):
    """Hands an uncaught error to the GUI thread.

    PyQt calls ``sys.excepthook`` on the thread the error happened on: the GUI
    thread for a slot or an event handler, the worker for an error escaping a
    ``QThread.run()``. The hook used to build the popup and write to the log
    dock right there, and widgets touched from a worker crashed the whole app
    (every shell, transfer and mirror with it) instead of keeping it open.

    The relay lives on the GUI thread, so an error reported on a worker is
    queued to it and one reported on the GUI thread is shown at once, as
    before. Only text crosses threads: a traceback would keep the worker's
    frames, and every object in them, alive until the GUI thread let go.
    """

    raised = pyqtSignal(object, str, str)  # exception type, its message, the traceback

    def __init__(self):
        super().__init__()
        self.raised.connect(self._report)

    @pyqtSlot(object, str, str)
    def _report(self, exc_type, text, details):
        _report_error(exc_type, text, details)


def _install_excepthook():
    """Uncaught errors -> crash.log, the log panel and one non-fatal popup,
    instead of crashing the app.

    The hook itself only writes crash.log, which is safe on any thread; the
    log panel and the popup are reached through :class:`_ErrorRelay`, on the
    GUI thread. Without a QApplication there is nothing to show them in, and
    crash.log is all there is.
    """
    global _error_relay
    app = QApplication.instance()
    if app is not None and _error_relay is None:
        relay = _ErrorRelay()
        if relay.thread() != app.thread():
            relay.moveToThread(app.thread())
        _error_relay = relay

    def hook(exc_type, exc, tb):
        details = "".join(traceback.format_exception(exc_type, exc, tb))
        _crash_log(details)
        relay = _error_relay
        if relay is None or QApplication.instance() is None:
            return
        try:
            text = str(exc)
        except Exception:  # an exception whose own __str__ fails
            text = object.__repr__(exc)
        try:
            relay.raised.emit(exc_type, text, details)
        except Exception as relay_exc:  # never recurse into the hook
            _crash_log(f"[excepthook] could not report the error: {relay_exc!r}")

    sys.excepthook = hook


def _sweep_stale_logs():
    """Scrollback temp files are removed on clean close; after a crash they
    linger in ~/.turboadb/logs forever — drop old files and empty remnants.

    Only files no running TurboADB owns (see ``scrollback.OWNER``): a second
    instance started while the first runs used to delete the first one's live
    history, an empty file (a history just started) or one idle for days."""
    import glob
    import time

    try:
        from .scrollback import forget_owners, history_owner, owner_running

        folder = os.path.join(_flag_dir(), "logs")
        cutoff = time.time() - 3 * 86400
        running = {}  # owner -> it still runs
        for p in glob.glob(os.path.join(folder, "turboadb-*.log")):
            owner = history_owner(p)
            if owner is not None:
                if owner not in running:
                    running[owner] = owner_running(folder, owner)
                if running[owner]:
                    continue
            try:
                # A zero-byte file has no user-visible history to preserve and
                # is normally a remnant from an interrupted terminal startup.
                if os.path.getsize(p) == 0 or os.path.getmtime(p) < cutoff:
                    os.remove(p)
            except OSError:
                pass
        forget_owners(folder)
    except Exception:
        pass


def _first_run_tasks():
    """Record first launch without opening a surprise second window.

    Documentation remains available from Help.  Opening a browser during startup
    made it look as though TurboADB had launched twice and could steal focus from
    the device window.
    """
    try:
        from . import settings as settings_mod

        settings_mod.load()  # validate settings before recording first launch
        folder = _flag_dir()
        os.makedirs(folder, exist_ok=True)
        flag = os.path.join(folder, "first-run-done")
        if os.path.exists(flag):
            return
        with open(flag, "w") as fh:
            fh.write("1")
    except Exception:
        pass


def _set_app_user_model_id():
    """Tell Windows this process is its OWN app (not generic python/pythonw), so
    the taskbar groups its windows under our icon instead of a blank/Python
    one. Must run before any window is created.  (Task Manager names a process
    after its program file: that is TurboADB.exe's part, see launcher.)"""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        from ..winshell import APP_ID

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    except Exception:
        pass


def _prewarm_adb_server() -> None:
    """Ask ADB to start before constructing the (larger) main window.

    A plain daemon thread: building the UI must never wait for USB
    enumeration.  It is the one early start of
    :func:`adb_path.prewarm_adb_server`, which ``turboadb-gui`` may already
    have made before PyQt was imported; MainWindow's startup check and the
    device tabs then wait on the same ``adb start-server`` launcher.
    """
    from .adb_path import prewarm_adb_server

    prewarm_adb_server()


def _show_already_running() -> None:
    QMessageBox.information(
        None,
        "TurboADB already running",
        "TurboADB is already open. Use the existing window instead of starting a second "
        "ADB tracker and device poller.",
    )


def _acquire_instance_lock() -> bool:
    """Allow one desktop GUI process, recovering only genuinely stale locks.

    Windows gets an OS mutex first (an atomic cross-process primitive), followed
    by QLockFile for cross-platform protection and recovery from stale locks.
    """
    global _instance_lock, _instance_mutex
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.windll.kernel32
            kernel32.CreateMutexW.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
            kernel32.CreateMutexW.restype = wintypes.HANDLE
            kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
            kernel32.CloseHandle.restype = wintypes.BOOL
            kernel32.GetLastError.restype = wintypes.DWORD
            mutex = kernel32.CreateMutexW(None, False, r"Local\TurboADB-GUI")
            if not mutex:
                raise OSError("CreateMutexW failed")
            if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
                kernel32.CloseHandle(mutex)
                _show_already_running()
                return False
            _instance_mutex = mutex
        except Exception:
            # QLockFile below remains a safe fallback on restrictive systems.
            _instance_mutex = None
    try:
        folder = _flag_dir()
        os.makedirs(folder, exist_ok=True)
        lock = QLockFile(os.path.join(folder, "turboadb-gui.lock"))
        lock.setStaleLockTime(10_000)
        if not lock.tryLock(100) and lock.removeStaleLockFile():
            lock.tryLock(100)
        if lock.isLocked():
            _instance_lock = lock
            return True
    except Exception:
        # A lock is a duplicate-launch safeguard, not a reason to make the GUI
        # unavailable on filesystems where Qt cannot create one.
        return True
    _release_instance_lock()
    _show_already_running()
    return False


def _stop_started_tools() -> None:
    """On the way out, close what TurboADB started: any scrcpy window it opened
    and the adb server it started.

    A mirror window or an adb daemon left running keeps the device busy after
    the app is gone. Settings → Startup turns both off.  A server TurboADB did
    not start (Android Studio's, a script's, one already running when it
    opened) is never stopped, and neither is one shared with other machines
    (``turboadb serve``): it exists for them, not for this app.  The
    PowerShell / CMD terminals the closing tabs ended are waited for (briefly),
    so no shell or command they ran outlives the app.
    """
    log = logging.getLogger("turboadb.gui")
    try:
        from .local_terminal import join_closers

        left = join_closers(3.0)
        if left:
            log.warning("%d local shell(s) were still being closed on exit", left)
    except Exception:
        pass
    try:
        from . import settings as settings_mod

        stop = settings_mod.get("stop_adb_on_exit", True)
    except Exception:
        stop = True
    if stop:
        try:
            from .. import scrcpy

            closed = scrcpy.stop_all()
            if closed:
                log.info("closed %d scrcpy window(s) on exit", closed)
        except Exception as exc:
            log.warning("could not close scrcpy on exit: %s", exc)
        try:
            _stop_adb_server(log)
        except Exception as exc:
            log.warning("could not stop the adb server on exit: %s", exc)
    else:
        log.info("left scrcpy and the adb server running (Settings → Startup)")
    try:
        from ..tools import end_server_launchers, server_lock_patience

        with server_lock_patience(1.0):
            end_server_launchers()  # a launcher still starting a server, never the server
    except Exception:
        pass


# How long the way out waits on the adb server: another thread can hold its
# lock across a blocking adb call (a kill waits up to 20 s, a shared-server
# start 10 s more, a closing tab's disconnect about 10 s).  Past this the
# server is left running rather than keeping a closed app alive.
_EXIT_SERVER_WAIT_S = 4.0


def _stop_adb_server(log) -> None:
    from ..tools import server_lock_patience

    with server_lock_patience(_EXIT_SERVER_WAIT_S):
        _stop_owned_adb_server(log)


def _stop_owned_adb_server(log) -> None:
    from .. import tools
    from ..config import ADBConfig
    from ..core import ADBHandler
    from ..devices import server_is_shared
    from .adb_path import gui_adb_path

    # A server this process is still starting is its own once it answers.
    tools.settle_adb_server_start(timeout=3.0)
    if not tools.is_adb_server_alive():
        return
    if server_is_shared():
        log.info("the adb server is shared with other machines — left running")
        return
    adb = tools.owned_adb_server()
    if not adb:
        log.info("the adb server was not started by TurboADB — left running")
        return
    if not os.path.exists(adb):
        # Never let a missing adb start the managed-tools download on the way
        # out; without a binary there is nothing of ours to stop anyway.
        adb = gui_adb_path()
        if not adb or not os.path.exists(adb):
            return
    result = ADBHandler(ADBConfig(adb_path=adb), quiet=True).stop_server(safe=True)
    if not result.success:
        log.warning("could not stop the adb server on exit: %s", result.error)
    elif result.value is not True:
        log.warning("the adb server was still running after kill-server")


def _release_instance_lock() -> None:
    global _instance_lock, _instance_mutex
    if _instance_lock is not None:
        try:
            _instance_lock.unlock()
        except Exception:
            pass
        _instance_lock = None
    if _instance_mutex is not None:
        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.windll.kernel32
            kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
            kernel32.CloseHandle.restype = wintypes.BOOL
            kernel32.CloseHandle(_instance_mutex)
        except Exception:
            pass
        _instance_mutex = None


def main():
    _set_app_user_model_id()
    app = QApplication(sys.argv)
    app.setApplicationName("TurboADB")
    app.setApplicationDisplayName("TurboADB")
    if os.path.exists(ICON_PATH):
        icon = QIcon(ICON_PATH)
        app.setWindowIcon(icon)
    if not _acquire_instance_lock():
        return 0
    from . import theme, settings as settings_mod

    # Start USB/daemon work at application T=0, in parallel with stylesheet and
    # window construction.  This commonly hides the whole UI-build interval on
    # cold ADB launches without ever blocking first paint.
    _prewarm_adb_server()
    # Stylesheet plus the placeholder-text palette role (not reachable via QSS).
    theme.apply_to_app(app, settings_mod.get("theme"))
    _install_app_logging()
    _install_excepthook()
    _first_run_tasks()
    _sweep_stale_logs()

    global _window
    _window = MainWindow()
    # The taskbar's icon for the Windows mode, and what a pin shows and starts:
    # set before the window shows, as Windows reads them then
    from . import taskbar

    _window._taskbar = taskbar.install(app, _window)
    # Always open maximized; the workspace (sidebar, screen and controls side by
    # side) is designed for the full screen.
    _window.showMaximized()
    try:
        return app.exec_()
    finally:
        _stop_started_tools()
        _release_instance_lock()


if __name__ == "__main__":
    raise SystemExit(main())
