"""GUI entry point: builds the QApplication, installs a crash-safe exception
hook (popup + log, non-fatal), runs first-run tasks, applies the saved theme at
the app level, and shows the main window."""

from __future__ import annotations

import os
import sys
import traceback

from PyQt5.QtCore import QLockFile
from PyQt5.QtGui import QIcon
from PyQt5.QtWidgets import QApplication, QMessageBox

from ..config import user_path
from .main_window import MainWindow, ICON_PATH

_FLAG_DIR = user_path()
_window = None  # set after creation, used by the exception hook
_instance_lock = None  # kept alive for the lifetime of the GUI process
_instance_mutex = None  # Windows' atomic duplicate-launch guard
_adb_prewarm_thread = None  # keeps the early daemon warm-up alive


def _crash_log(text: str):
    try:
        os.makedirs(_FLAG_DIR, exist_ok=True)
        with open(os.path.join(_FLAG_DIR, "crash.log"), "a", encoding="utf-8") as fh:
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
        os.makedirs(_FLAG_DIR, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            os.path.join(_FLAG_DIR, "turboadb.log"),
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
    # worker-thread exceptions -> crash.log too (the Qt excepthook only covers
    # the UI thread)
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


def _on_error_box_closed(*_):
    global _error_box, _suppressed_errors
    _error_box = None
    _suppressed_errors = 0


def _show_error_popup(exc_type, exc) -> None:
    """Show ONE non-modal error popup; further errors update it instead of
    stacking a new modal box per exception (a repeating timer error used to
    bury the window under dialogs)."""
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


def _install_excepthook():
    """Uncaught GUI-thread errors -> log to the panel + a non-fatal popup,
    instead of crashing the app."""

    def hook(exc_type, exc, tb):
        msg = "".join(traceback.format_exception(exc_type, exc, tb))
        _crash_log(msg)
        if _window is not None:
            try:
                _window.log_panel.append(f"[ERROR] Unexpected error:\n{msg.rstrip()}")
            except Exception:
                pass
        try:
            _show_error_popup(exc_type, exc)
        except Exception as popup_exc:  # never recurse into the hook
            _crash_log(f"[excepthook] could not show the error popup: {popup_exc!r}")

    sys.excepthook = hook


def _sweep_stale_logs():
    """Scrollback temp files are removed on clean close; after a crash they
    linger in ~/.turboadb/logs forever — drop old files and empty remnants."""
    import glob
    import time

    try:
        cutoff = time.time() - 3 * 86400
        for p in glob.glob(os.path.join(_FLAG_DIR, "logs", "turboadb-*.log")):
            try:
                # A zero-byte file has no user-visible history to preserve and
                # is normally a remnant from an interrupted terminal startup.
                if os.path.getsize(p) == 0 or os.path.getmtime(p) < cutoff:
                    os.remove(p)
            except OSError:
                pass
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
        os.makedirs(_FLAG_DIR, exist_ok=True)
        flag = os.path.join(_FLAG_DIR, "first-run-done")
        if os.path.exists(flag):
            return
        with open(flag, "w") as fh:
            fh.write("1")
    except Exception:
        pass


def _set_app_user_model_id():
    """Tell Windows this process is its OWN app (not generic python/pythonw), so
    the taskbar and Task Manager use our icon instead of a blank/Python one. Must
    run before any window is created."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("TurboADB.DeviceToolkit")
    except Exception:
        pass


def _prewarm_adb_server() -> None:
    """Ask ADB to start before constructing the (larger) main window.

    This is deliberately a plain daemon thread: building the UI must never wait
    for USB enumeration, and :func:`ensure_adb_server` already owns a process-
    local lock so MainWindow's later verification joins the same launch rather
    than racing it.
    """
    global _adb_prewarm_thread
    if _adb_prewarm_thread is not None and _adb_prewarm_thread.is_alive():
        return

    def run() -> None:
        try:
            from ..tools import ensure_adb_server
            from .adb_path import gui_adb_path

            # Do not let a system/PATH ADB win this race.  The rest of the GUI
            # is pinned to this same adb (Settings path, else managed copy).
            adb_path = gui_adb_path()
            # A pre-warm is a latency optimisation, never a reason to leave a
            # launch hint spinning for many seconds when the daemon is broken.
            ensure_adb_server(adb_path, timeout=2.5)
        except Exception:
            # The normal GUI worker reports an actionable message after the
            # window exists.  This early helper is only a latency optimisation.
            pass

    import threading

    _adb_prewarm_thread = threading.Thread(
        target=run,
        name="TurboADB-early-server-start",
        daemon=True,
    )
    _adb_prewarm_thread.start()


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
        os.makedirs(_FLAG_DIR, exist_ok=True)
        lock = QLockFile(os.path.join(_FLAG_DIR, "turboadb-gui.lock"))
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
    # Always open maximized; the workspace (sidebar, screen and controls side by
    # side) is designed for the full screen.
    _window.showMaximized()
    try:
        return app.exec_()
    finally:
        _release_instance_lock()


if __name__ == "__main__":
    raise SystemExit(main())
