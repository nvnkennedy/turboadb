"""GUI entry point: builds the QApplication, installs a crash-safe exception
hook (popup + log, non-fatal), runs first-run tasks, applies the saved theme at
the app level, and shows the main window."""

from __future__ import annotations

import os
import sys
import traceback
import webbrowser

from PyQt5.QtGui import QIcon
from PyQt5.QtWidgets import QApplication, QMessageBox

from .main_window import MainWindow, ICON_PATH

DOCS_URL = "https://pypi.org/project/turboadb/"
_FLAG_DIR = os.path.join(os.path.expanduser("~"), ".turboadb")
_window = None          # set after creation, used by the exception hook


def _crash_log(text: str):
    try:
        os.makedirs(_FLAG_DIR, exist_ok=True)
        with open(os.path.join(_FLAG_DIR, "crash.log"), "a", encoding="utf-8") as fh:
            fh.write(text + "\n")
    except Exception:
        pass


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
            QMessageBox.warning(_window, "TurboADB — error",
                                f"{exc_type.__name__}: {exc}\n\n"
                                "The app stayed open; details are in the log.")
        except Exception:
            pass
    sys.excepthook = hook


def _first_run_tasks():
    """Open the docs the first time the app runs. (Desktop + Start-menu shortcuts
    are created/refreshed every launch by the main window, so they self-heal.)"""
    try:
        from . import settings as settings_mod
        cfg = settings_mod.load()
        os.makedirs(_FLAG_DIR, exist_ok=True)
        flag = os.path.join(_FLAG_DIR, "first-run-done")
        if os.path.exists(flag):
            return
        if cfg.get("open_docs_first_run", True):
            webbrowser.open(DOCS_URL)
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
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "TurboADB.DeviceToolkit")
    except Exception:
        pass


def main():
    _set_app_user_model_id()
    app = QApplication(sys.argv)
    app.setApplicationName("TurboADB")
    app.setApplicationDisplayName("TurboADB")
    if os.path.exists(ICON_PATH):
        icon = QIcon(ICON_PATH)
        app.setWindowIcon(icon)
    from . import theme, settings as settings_mod
    app.setStyleSheet(theme.stylesheet(settings_mod.get("theme")))
    # placeholder text colour isn't reachable via stylesheet — set the palette
    # role so input hints are clearly visible (they were nearly invisible)
    try:
        from PyQt5.QtGui import QPalette, QColor
        pal = app.palette()
        pal.setColor(QPalette.PlaceholderText, QColor("#9aa0a6"))
        app.setPalette(pal)
    except Exception:
        pass
    _install_excepthook()
    _first_run_tasks()

    global _window
    _window = MainWindow()
    _window.show()
    return app.exec_()


if __name__ == "__main__":
    raise SystemExit(main())
