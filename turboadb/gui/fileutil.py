"""Small file helpers for the GUI: open a saved file, reveal it in the file
manager, and a 'Saved' popup offering both."""

from __future__ import annotations

import os
import sys
import subprocess
import html


def download_dir() -> str:
    """The user's Downloads folder — where saved files SHOULD land (screenshots,
    recordings, pulled files, logs…), not some arbitrary place. Falls back to
    ~/Downloads, then the home directory, and always returns a real directory."""
    d = ""
    try:
        from PyQt5.QtCore import QStandardPaths

        d = QStandardPaths.writableLocation(QStandardPaths.DownloadLocation) or ""
    except Exception:
        d = ""
    if not d or not os.path.isdir(d):
        cand = os.path.join(os.path.expanduser("~"), "Downloads")
        d = cand if os.path.isdir(cand) else os.path.expanduser("~")
    return d


def download_path(filename: str) -> str:
    """A full path in the Downloads folder for *filename* (a bare name)."""
    return os.path.join(download_dir(), filename)


def open_path(path: str) -> None:
    """Open *path* with its default application."""
    try:
        if sys.platform == "win32":
            os.startfile(path)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception:
        pass


def reveal_path(path: str) -> None:
    """Show *path* selected in the OS file manager (Explorer / Finder / …)."""
    path = os.path.normpath(path)
    try:
        if sys.platform == "win32":
            # explorer needs the comma glued to /select and a quoted path
            subprocess.Popen('explorer /select,"%s"' % path)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", path])
        else:
            subprocess.Popen(["xdg-open", os.path.dirname(path) or "."])
    except Exception:
        pass


LOG_FILE_FILTER = "Log files (*.log);;Text files (*.txt);;All files (*)"


def _alive(widget) -> bool:
    """True while *widget*'s C++ object still exists (None counts as alive)."""
    if widget is None:
        return True
    try:
        from PyQt5 import sip

        return not sip.isdeleted(widget)
    except Exception:  # pragma: no cover - sip always ships with PyQt5
        return True


def ask_save_path(parent, title: str, default_name: str, file_filter: str = LOG_FILE_FILTER,
                  suffix: str = "") -> str:
    """Ask for a destination (pre-filled in Downloads); '' when cancelled."""
    from PyQt5.QtWidgets import QFileDialog

    path, _ = QFileDialog.getSaveFileName(parent, title, download_path(default_name), file_filter)
    if path and suffix and not os.path.splitext(path)[1]:
        path += suffix
    return path or ""


def write_file_async(parent, path: str, write, *, what: str = "file", title: str = "Save",
                     on_saved=None, on_failed=None, notify: bool = True):
    """Run ``write(path)`` off the UI thread, then report the outcome.

    Success shows a non-blocking saved toast (and calls ``on_saved(path)``);
    failure shows a warning with the real error (and calls
    ``on_failed(message)``).  If *parent* was closed in the meantime the
    outcome is still reported (unparented) but the callbacks are skipped.
    Snapshot any widget state BEFORE calling this — *write* runs on a worker
    thread and must not touch widgets.
    """
    from .qtutil import FunctionThread, park_thread

    job = FunctionThread(lambda: write(path))

    def done(_result):
        alive = _alive(parent)
        if notify:
            saved_toast(parent if alive else None, path, what)
        if alive and on_saved is not None:
            on_saved(path)

    def fail(message):
        from PyQt5.QtWidgets import QMessageBox

        alive = _alive(parent)
        QMessageBox.warning(
            parent if alive else None, title, f"Could not save the {what}:\n{path}\n\n{message}"
        )
        if alive and on_failed is not None:
            on_failed(message)

    job.done.connect(done)
    job.fail.connect(fail)
    park_thread(job)  # referenced until finished, even if *parent* closes
    job.start()
    return job


def save_output(parent, title: str, default_name: str, write, *, what: str = "file",
                file_filter: str = LOG_FILE_FILTER, suffix: str = "", on_saved=None,
                on_failed=None):
    """The one "Save output…" flow: ask for a path, write it off the UI thread,
    show the saved toast or a real error.  Returns the chosen path ('' if
    cancelled); the write itself completes asynchronously."""
    path = ask_save_path(parent, title, default_name, file_filter, suffix)
    if path:
        write_file_async(parent, path, write, what=what, title=title,
                         on_saved=on_saved, on_failed=on_failed)
    return path


def write_text_file(path: str, text: str, newline: str = "") -> None:
    """Write *text* as UTF-8 (``newline=''`` keeps line endings exactly)."""
    with open(path, "w", encoding="utf-8", newline=newline) as fh:
        fh.write(text)


def notification_toast(parent, title: str, message: str, *, action_text: str = "", action=None, duration_ms: int = 7000, rich: bool = False):
    """Show a compact non-blocking notification anchored to the parent window."""
    from PyQt5.QtCore import Qt, QTimer
    from PyQt5.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout

    host = parent.window() if parent is not None else None
    toast = QDialog(host, Qt.Tool | Qt.FramelessWindowHint)
    toast.setModal(False)
    toast.setAttribute(Qt.WA_DeleteOnClose, True)
    toast.setObjectName("notificationToast")
    layout = QVBoxLayout(toast)
    layout.setContentsMargins(14, 11, 12, 10)
    layout.setSpacing(5)
    heading = QLabel(title)
    heading.setObjectName("notificationToastTitle")
    body = QLabel(message)
    body.setObjectName("notificationToastBody")
    body.setTextFormat(Qt.RichText if rich else Qt.PlainText)
    body.setWordWrap(True)
    body.setMaximumWidth(420)
    layout.addWidget(heading)
    layout.addWidget(body)
    buttons = QHBoxLayout()
    buttons.addStretch(1)
    if action_text and action is not None:
        button = QPushButton(action_text)
        button.setProperty("role", "ghost")

        def trigger_action():
            # Run the callback before closing the tool window.  On Windows a
            # close may synchronously deactivate the parent, which used to make
            # “Show log” appear to do nothing for a hidden dock.
            try:
                action()
            finally:
                toast.close()

        button.clicked.connect(trigger_action)
        buttons.addWidget(button)
    close = QPushButton("×")
    close.setToolTip("Dismiss")
    close.setProperty("role", "ghost")
    close.clicked.connect(toast.close)
    buttons.addWidget(close)
    layout.addLayout(buttons)

    # Keep transient notifications alive and stack newer ones above older ones.
    bucket = getattr(host, "_turboadb_toasts", []) if host is not None else []
    bucket[:] = [item for item in bucket if item is not None and item.isVisible()]
    if host is not None:
        host._turboadb_toasts = bucket
    toast.adjustSize()
    if host is not None:
        point = host.mapToGlobal(host.rect().bottomRight())
        y = point.y() - toast.height() - 34
        for item in bucket:
            y -= item.height() + 8
        toast.move(point.x() - toast.width() - 18, max(8, y))
    bucket.append(toast)
    toast.finished.connect(lambda *_: bucket.remove(toast) if toast in bucket else None)
    toast.show()
    # A child timer dies with the toast; a bare singleShot(toast.close) fired
    # into an already-deleted (WA_DeleteOnClose) toast after a manual dismiss.
    timer = QTimer(toast)
    timer.setSingleShot(True)
    timer.timeout.connect(toast.close)
    timer.start(duration_ms)
    return toast


def saved_toast(parent, path: str, what: str = "file") -> None:
    """Non-blocking saved notification with a clickable file name and Open action."""
    escaped = html.escape(os.path.basename(path) or path)
    message = f'<a href="open">{escaped}</a><br><span>{html.escape(path)}</span>'
    toast = notification_toast(
        parent,
        f"Saved {what}",
        message,
        action_text="Open",
        action=lambda: open_path(path),
        rich=True,
    )
    # QLabel treats the message as rich text; wire the hyperlink to the same
    # predictable native opener as the Open button.
    from PyQt5.QtCore import Qt
    from PyQt5.QtWidgets import QLabel

    for label in toast.findChildren(QLabel):
        if label.objectName() == "notificationToastBody":
            label.setTextInteractionFlags(Qt.TextBrowserInteraction)
            label.linkActivated.connect(lambda *_: open_path(path))
            break


def _alive(widget) -> bool:
    try:
        widget.isVisible()
        return True
    except RuntimeError:  # the C++ widget is gone
        return False


def _toast_window(host, name: str):
    """A frameless tool window above *host* that never steals keyboard focus
    (typing into a terminal or the device screen must keep working)."""
    from PyQt5.QtCore import Qt, QTimer
    from PyQt5.QtWidgets import QFrame

    toast = QFrame(host, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowDoesNotAcceptFocus)
    toast.setObjectName(name)
    toast.setAttribute(Qt.WA_ShowWithoutActivating, True)
    toast.setAttribute(Qt.WA_StyledBackground, True)
    toast.timer = QTimer(toast)
    toast.timer.setSingleShot(True)
    toast.timer.timeout.connect(toast.hide)
    toast.on_action = None
    return toast


def _copy_text(text: str) -> None:
    from PyQt5.QtWidgets import QApplication

    QApplication.clipboard().setText(text or "")


def _fit_toast(toast, width=None) -> None:
    """Size a toast with word-wrapped text to its content in one step.

    ``adjustSize()`` on a frameless top-level window asked Windows for a height
    that ignored the wrapped lines; Windows grew the window and Qt printed
    "QWindowsWindow::setGeometry: Unable to set geometry" on every toast.
    """
    layout = toast.layout()
    # Stylesheet padding and fonts apply at polish time; measuring an
    # unpolished toast gave a height the shown window then outgrew.
    toast.ensurePolished()
    layout.activate()
    if width is None:
        width = layout.sizeHint().width()
    width = max(int(width), toast.minimumSizeHint().width())
    if layout.hasHeightForWidth():
        height = layout.totalHeightForWidth(width)
    else:
        height = layout.totalSizeHint().height()
    height = max(height, toast.minimumSizeHint().height())
    toast.setFixedSize(width, height)


def _mark(glyph: str, tone: str, size: int):
    from PyQt5.QtCore import QSize, Qt
    from PyQt5.QtWidgets import QToolButton

    from .icons import icon

    mark = QToolButton()
    mark.setIcon(icon(glyph, tone))
    mark.setIconSize(QSize(size, size))
    mark.setAttribute(Qt.WA_TransparentForMouseEvents, True)
    mark.setFocusPolicy(Qt.NoFocus)
    return mark


def activity_toast(parent, message: str, *, level: str = "info", action_text: str = "",
                   action=None, duration_ms=None):
    """A small toast at the bottom right for what the app just did.

    One toast per window: a new message replaces the text and restarts the
    timer instead of stacking popups. *level* is ``info``, ``ok`` or ``warning``.
    """
    from PyQt5.QtWidgets import QHBoxLayout, QLabel, QPushButton

    from .icons import icon

    host = parent.window() if parent is not None else None
    if host is None:
        return None
    toast = getattr(host, "_turboadb_activity_toast", None)
    if toast is None or not _alive(toast):
        toast = _toast_window(host, "activityToast")
        row = QHBoxLayout(toast)
        row.setContentsMargins(12, 9, 8, 9)
        row.setSpacing(8)
        toast.mark = _mark("info", "accent", 18)
        toast.mark.setObjectName("toastMark")
        toast.text = QLabel()
        toast.text.setObjectName("activityToastText")
        toast.text.setWordWrap(True)
        toast.text.setMaximumWidth(460)
        toast.action = QPushButton()
        toast.action.setProperty("role", "ghost")
        toast.action.clicked.connect(lambda: toast.on_action and toast.on_action())
        close = QPushButton()
        close.setProperty("role", "ghost")
        close.setIcon(icon("x"))
        close.setToolTip("Dismiss")
        close.clicked.connect(toast.hide)
        for widget in (toast.mark, toast.text, toast.action, close):
            row.addWidget(widget)
        host._turboadb_activity_toast = toast
    glyph, tone = {"ok": ("check", "ok"), "warning": ("alert", "warn")}.get(level, ("info", "accent"))
    toast.mark.setIcon(icon(glyph, tone))
    toast.text.setText(message)
    toast.on_action = action
    toast.action.setText(action_text)
    toast.action.setVisible(bool(action_text and action is not None))
    # Go straight from the old fixed size to the new one: releasing the size
    # limits first let a visible toast shrink below what Windows accepts and
    # printed "Unable to set geometry".
    _fit_toast(toast)
    corner = host.mapToGlobal(host.rect().bottomRight())
    toast.move(corner.x() - toast.width() - 18, corner.y() - toast.height() - 40)
    toast.show()
    toast.raise_()
    toast.timer.start(duration_ms or (8000 if level == "warning" else 4000))
    return toast


_LAST_ERROR_SOUND = [float("-inf")]


def _play_error_sound() -> None:
    try:
        if sys.platform == "win32":
            import winsound

            winsound.MessageBeep(winsound.MB_ICONHAND)
            return
    except Exception:
        pass
    try:
        from PyQt5.QtWidgets import QApplication

        QApplication.beep()
    except Exception:
        pass


def _error_beep() -> None:
    """One alert sound per burst of errors (at most every 1.5 s)."""
    import time

    now = time.monotonic()
    if now - _LAST_ERROR_SOUND[0] < 1.5:
        return
    _LAST_ERROR_SOUND[0] = now
    _play_error_sound()


def error_toast(parent, message: str, *, title: str = "Error", action_text: str = "Show log",
                action=None, duration_ms: int = 12000):
    """A large red toast at the bottom centre of the window, with a beep.

    Errors arriving while it is up update it in place and count them, so a
    burst of failures is one popup and one sound, not a pile of both.
    """
    from PyQt5.QtCore import Qt
    from PyQt5.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout

    host = parent.window() if parent is not None else None
    if host is None:
        return None
    toast = getattr(host, "_turboadb_error_toast", None)
    if toast is None or not _alive(toast):
        toast = _toast_window(host, "errorToast")
        box = QVBoxLayout(toast)
        box.setContentsMargins(18, 14, 14, 12)
        box.setSpacing(6)
        head = QHBoxLayout()
        head.setSpacing(8)
        head.addWidget(_mark("alert", "on-danger", 22))
        toast.title = QLabel()
        toast.title.setObjectName("errorToastTitle")
        head.addWidget(toast.title, 1)
        # The toast never takes keyboard focus (typing must stay in the
        # terminal or on the device screen), so Ctrl+C would reach the
        # terminal and stop its command. Copy the message with a button.
        copy = QPushButton("Copy")
        copy.setToolTip("Copy the error message")
        copy.clicked.connect(lambda: _copy_text(toast.text.text()))
        head.addWidget(copy)
        close = QPushButton("Dismiss")
        close.clicked.connect(toast.hide)
        head.addWidget(close)
        box.addLayout(head)
        toast.text = QLabel()
        toast.text.setObjectName("errorToastText")
        toast.text.setWordWrap(True)
        toast.text.setTextInteractionFlags(Qt.TextSelectableByMouse)
        box.addWidget(toast.text)
        row = QHBoxLayout()
        row.addStretch(1)
        toast.action = QPushButton()
        toast.action.clicked.connect(lambda: toast.on_action and toast.on_action())
        row.addWidget(toast.action)
        box.addLayout(row)
        toast.count = 0
        host._turboadb_error_toast = toast
    toast.count = toast.count + 1 if toast.isVisible() else 1
    toast.title.setText(title if toast.count == 1 else f"{title} ({toast.count})")
    toast.text.setText(message)
    toast.on_action = action
    toast.action.setText(action_text)
    toast.action.setVisible(bool(action_text and action is not None))
    _fit_toast(toast, min(680, max(420, host.width() - 160)))
    origin = host.mapToGlobal(host.rect().bottomLeft())
    toast.move(origin.x() + (host.width() - toast.width()) // 2,
               origin.y() - toast.height() - 44)
    toast.show()
    toast.raise_()
    toast.timer.start(duration_ms)
    _error_beep()
    return toast


def saved_dialog(parent, path: str, what: str = "file") -> None:
    """After saving, show a popup with the path (as a clickable link) and
    Open file / Open folder / Close buttons."""
    from PyQt5.QtCore import Qt, QUrl
    from PyQt5.QtWidgets import QMessageBox, QLabel

    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Information)
    box.setWindowTitle("Saved")
    box.setTextFormat(Qt.RichText)
    box.setText(f"Saved {html.escape(what)}.")
    # A properly percent-encoded file URL (spaces, '#', '%' in names) and an
    # escaped label, so '<' or '&' in a path can never inject markup.
    href = QUrl.fromLocalFile(os.path.abspath(path)).toString(QUrl.FullyEncoded)
    box.setInformativeText(
        f'<a href="{html.escape(href, quote=True)}">{html.escape(path)}</a>'
    )
    # make the file link actually clickable (opens the file in its default app)
    for lbl in box.findChildren(QLabel):
        lbl.setOpenExternalLinks(True)
        lbl.setTextInteractionFlags(lbl.textInteractionFlags() | Qt.TextBrowserInteraction)
    b_open = box.addButton("Open file", QMessageBox.AcceptRole)
    b_folder = box.addButton("Open folder", QMessageBox.ActionRole)
    box.addButton("Close", QMessageBox.RejectRole)
    box.setDefaultButton(b_open)
    box.exec_()
    clicked = box.clickedButton()
    # the box has a parent, so without this every save leaked one dialog
    box.deleteLater()
    if clicked is b_open:
        open_path(path)
    elif clicked is b_folder:
        reveal_path(path)
