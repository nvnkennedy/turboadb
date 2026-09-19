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
    _clean_copy_menu(body, lambda: _plain_text(message) if rich else message)
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
    bucket[:] = [item for item in bucket if item is not None and _alive(item) and item.isVisible()]
    if host is not None:
        host._turboadb_toasts = bucket
        _fit_notification(toast, body, host, message, rich)
    else:
        toast.adjustSize()
    bucket.append(toast)
    toast.finished.connect(lambda *_: bucket.remove(toast) if toast in bucket else None)
    if host is not None:
        # One bottom-right column with the activity toast, clear of the error toast.
        _arrange_toasts(host, showing=toast)
    toast.show()
    # A child timer dies with the toast; a bare singleShot(toast.close) fired
    # into an already-deleted (WA_DeleteOnClose) toast after a manual dismiss.
    timer = QTimer(toast)
    timer.setSingleShot(True)
    timer.timeout.connect(toast.close)
    timer.start(duration_ms)
    return toast


def _fit_notification(toast, body, host, message: str, rich: bool) -> None:
    """Size a notification to its body text on one line (a saved file's path),
    wrapping only past the widest allowed width, like the activity toast."""
    toast.ensurePolished()
    anchor, work = _toast_area(host)
    margins = toast.layout().contentsMargins()
    chrome = margins.left() + margins.right()
    scale = _font_scale(body)
    widest = round(_NOTIFICATION_TEXT_MAX * scale)
    limit = min(widest, anchor.width() - 2 * _TOAST_SIDE - chrome)
    limit = max(limit, min(round(_ACTIVITY_TEXT_MIN * scale), widest))
    limit = max(40, min(limit, work.width() - chrome))
    room = limit - _label_extra(body)
    metrics = body.fontMetrics()
    # A long file name or path gets break points too; in rich text only its
    # text is touched, so the escaped markup stays valid.
    body.setText(_breakable_html(message, metrics, room) if rich
                 else _breakable(message, metrics, room))
    body.setMinimumWidth(0)
    body.setMaximumWidth(_QWIDGETSIZE_MAX)
    shown = _plain_text(body.text()) if rich else body.text()
    body.setFixedWidth(_one_line_width(body, limit, shown))
    _fit_toast(toast)


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
    toast.message = ""
    return toast


def _copy_text(text: str) -> None:
    from PyQt5.QtWidgets import QApplication

    QApplication.clipboard().setText(text or "")


# Toast geometry, in logical pixels.
_ACTIVITY_TEXT_MAX = 460   # widest one-line activity message; longer ones wrap
_ACTIVITY_TEXT_MIN = 200   # a narrow window still gives the text this much
_NOTIFICATION_TEXT_MAX = 520  # a "Saved …" notification's path, before it wraps
_ERROR_WIDTH = (420, 680)  # the error toast's width range on a roomy window
_TOAST_SIDE = 18           # gap between the activity toast and the window's right edge
_ACTIVITY_BOTTOM = 40      # gap above the window's bottom edge
_ERROR_BOTTOM = 44
_TOAST_GAP = 8             # between the activity toast and an error toast below it
_SCREEN_MARGIN = 8         # toasts keep this far inside the screen's work area
_QWIDGETSIZE_MAX = (1 << 24) - 1
# Longer messages are shortened on screen; the log (and the error toast's Copy
# button) keep all of them. A stack trace built a toast taller than the screen.
_ACTIVITY_LINES = 8
_ERROR_LINES = 14
# A warning stays readable this long before an info/OK message may replace it
# ("download failed" was gone the moment "Tools ready" followed).
_WARNING_HOLD_S = 2.5
# Updates this close together are one burst: the toast grows but does not shrink,
# instead of resizing and moving for every file of a long transfer.
_BURST_S = 1.0
_ERROR_BEEP_GAP_S = 1.5
_ZWSP = "\u200b"
_BREAK_AFTER = "\\/_.-:=&?,;|+"
_BREAK_RUN = 12            # code points between forced break points in a long run
_JOINERS = (0x200C, 0x200D)


def _strip_breaks(text: str) -> str:
    return (text or "").replace(_ZWSP, "")


def _may_break(before: str, after: str) -> bool:
    """False where a break (an inserted zero-width space) would change how the
    text renders: before a combining mark, variation selector or emoji
    modifier, after a virama (the next letter forms a conjunct), next to a
    zero-width (non-)joiner, inside a flag or a Hangul syllable."""
    import unicodedata

    b, a = ord(before), ord(after)
    if a in _JOINERS or b in _JOINERS:
        return False
    if unicodedata.category(after) in ("Mn", "Mc", "Me"):
        return False
    if unicodedata.combining(before) == 9:  # virama / halant
        return False
    if 0xFE00 <= a <= 0xFE0F or 0xE0100 <= a <= 0xE01EF or 0x1F3FB <= a <= 0x1F3FF:
        return False
    if 0x1F1E6 <= a <= 0x1F1FF and 0x1F1E6 <= b <= 0x1F1FF:
        return False
    return not 0x1160 <= a <= 0x11FF  # Hangul vowel / final jamo


def _break_points(word: str):
    """Indices of *word* before which a line may break: Qt's grapheme
    boundaries that :func:`_may_break` also allows."""
    from PyQt5.QtCore import QTextBoundaryFinder

    python_index, unit = {}, 0
    for index, ch in enumerate(word):  # Qt counts UTF-16 units, Python code points
        python_index[unit] = index
        unit += 2 if ord(ch) > 0xFFFF else 1
    finder = QTextBoundaryFinder(QTextBoundaryFinder.Grapheme, word)
    points = []
    position = finder.toNextBoundary()
    while position != -1:
        index = python_index.get(position)
        if index is not None and 0 < index < len(word) and _may_break(word[index - 1], word[index]):
            points.append(index)
        position = finder.toNextBoundary()
    return points


def _breakable(text: str, metrics, width: int) -> str:
    """*text* with invisible break points inside every word wider than *width*.

    A word-wrapped QLabel only breaks between words, so a long path or package
    name wider than the toast was cut off at its edge. Words that fit are left
    untouched, so selecting and copying ordinary text never picks up a
    zero-width space. Breaks go after separators and every ``_BREAK_RUN``
    code points, but only between whole characters (:func:`_break_points`),
    so a Devanagari conjunct or an accent is never split.
    """
    import re

    if width <= 0 or not text:
        return text

    def split(match):
        word = match.group(0)
        if metrics.horizontalAdvance(word) <= width:
            return word
        pieces, last = [], 0
        for point in _break_points(word):
            if word[point - 1] in _BREAK_AFTER or point - last >= _BREAK_RUN:
                pieces.append(word[last:point])
                last = point
        pieces.append(word[last:])
        return _ZWSP.join(pieces)

    return re.sub(r"\S+", split, text)


def _breakable_html(markup: str, metrics, width: int) -> str:
    """:func:`_breakable` for the text of a rich-text *markup*, leaving its tags
    and entities intact (text is unescaped, broken, then escaped again)."""
    import re

    parts = re.split(r"(<[^>]*>)", markup)
    for index in range(0, len(parts), 2):
        if parts[index]:
            text = html.unescape(parts[index])
            parts[index] = html.escape(_breakable(text, metrics, width), quote=False)
    return "".join(parts)


def _plain_text(markup: str) -> str:
    """The text a rich-text QLabel shows for *markup*, one line per paragraph."""
    from PyQt5.QtGui import QTextDocument

    document = QTextDocument()
    document.setHtml(markup)
    return document.toPlainText().replace("\u2028", "\n").replace("\u2029", "\n")


def _clean_copy_menu(label, full_text) -> None:
    """Right-click Copy / Select All for a toast's text without the invisible
    break points: a copied path must paste as the real path. Copy takes the
    selection, or the whole message (``full_text()``) when nothing is selected."""
    from PyQt5.QtCore import Qt
    from PyQt5.QtWidgets import QMenu

    def show(position):
        menu = QMenu(label)
        selected = label.selectedText()
        copy = menu.addAction("Copy")
        copy.triggered.connect(lambda: _copy_text(_strip_breaks(selected or full_text())))
        select_all = menu.addAction("Select All")
        select_all.triggered.connect(lambda: label.setSelection(0, len(
            _plain_text(label.text()) if label.textFormat() == Qt.RichText else label.text()
        )))
        menu.exec_(label.mapToGlobal(position))
        menu.deleteLater()

    label.setContextMenuPolicy(Qt.CustomContextMenu)
    label.customContextMenuRequested.connect(show)


def _shortened(text: str, metrics, width: int, max_lines: int, note: str) -> str:
    """*text* cut to at most *max_lines* wrapped lines at *width*: the part that
    fits, "…", and *note* on a line of its own. Unchanged when it fits."""
    from PyQt5.QtCore import QRect, Qt

    flags = int(Qt.AlignLeft | Qt.TextWordWrap)
    width = max(1, int(width))

    def height(candidate):
        return metrics.boundingRect(QRect(0, 0, width, 1 << 24), flags, candidate).height()

    limit = height("\n".join(["X"] * max(1, int(max_lines))))
    if height(text) <= limit:
        return text

    def cut(count):
        return text[:count].rstrip(" \t\r\n" + _ZWSP) + "…\n" + note

    low, high = 0, len(text)
    while low < high:  # the longest start of the text that still fits
        middle = (low + high + 1) // 2
        if height(cut(middle)) <= limit:
            low = middle
        else:
            high = middle - 1
    while 0 < low < len(text) and not _may_break(text[low - 1], text[low]):
        low -= 1  # never end inside a character (an accent, a conjunct)
    return cut(low)


def _fit_toast(toast, width=None, min_height: int = 0) -> None:
    """Size a toast with word-wrapped text to its content in one step.

    ``adjustSize()`` on a frameless top-level window asked Windows for a height
    that ignored the wrapped lines; Windows grew the window and Qt printed
    "QWindowsWindow::setGeometry: Unable to set geometry" on every toast.
    Going straight from the old fixed size to the new one also keeps a visible
    toast from shrinking below what Windows accepts in between.
    """
    from PyQt5.QtCore import QSize

    layout = toast.layout()
    # Stylesheet padding and fonts apply at polish time; measuring an
    # unpolished toast gave a height the shown window then outgrew.
    toast.ensurePolished()
    # Measure against the new text, not the previous message's fixed size, and
    # only drop the layout's cached hints: activating it would first resize a
    # shown toast to the layout minimum — a height that ignores the wrapped
    # lines and a width the old maximum forbids. Windows refuses that
    # in-between geometry ("QWindowsWindow::setGeometry: Unable to set
    # geometry ... minimum size: 706x51 maximum size: 263x51"). The one size
    # this call settles on is set at the end, before anything is painted.
    toast.setMinimumSize(0, 0)
    toast.setMaximumSize(_QWIDGETSIZE_MAX, _QWIDGETSIZE_MAX)
    layout.invalidate()
    if width is None:
        width = layout.totalSizeHint().width()
    width = max(int(width), toast.minimumSizeHint().width())
    if layout.hasHeightForWidth():
        height = layout.totalHeightForWidth(width)
    else:
        height = layout.totalSizeHint().height()
    height = max(height, toast.minimumSizeHint().height(), int(min_height))
    toast.setFixedSize(QSize(width, height))


def _font_scale(widget) -> float:
    """How much larger than at 96 DPI text is drawn (1.25 at 125% scaling).

    Without Qt's high-DPI scaling, fonts grow with the Windows scale factor
    while pixel sizes stay put; widths meant for a number of characters
    follow the font so a scaled-up message doesn't wrap sooner."""
    try:
        return max(1.0, widget.logicalDpiX() / 96.0)
    except Exception:
        return 1.0


def _label_extra(label) -> int:
    """Horizontal pixels a QLabel adds around its text (margins, indent)."""
    margins = label.contentsMargins()
    return margins.left() + margins.right() + 2 * max(0, label.margin()) + max(0, label.indent())


def _widest_run(metrics, text: str) -> int:
    """Width of the widest piece of *text* that cannot wrap (a word, a path)."""
    from PyQt5.QtCore import QRect, Qt

    flags = int(Qt.AlignLeft | Qt.TextWordWrap)
    return metrics.boundingRect(QRect(0, 0, 1, 1 << 24), flags, text).width()


def _one_line_width(label, limit: int, text=None) -> int:
    """The narrowest width at which *label* shows its text on as few lines as
    it would with unlimited room, or *limit* when the text has to wrap anyway.

    QLabel's own size hint for word-wrapped text deliberately picks a narrow
    column (a quarter or half of its maximum width), which wrapped "Wi-Fi on"
    and every sentence onto two or three short lines. *text* is what the label
    shows as plain text (needed for rich text); the label's own by default.
    The label's minimum width must be reset before measuring.
    """
    limit = max(1, int(limit))
    unlimited = label.heightForWidth(1 << 16)
    if label.heightForWidth(limit) > unlimited:
        return limit  # wraps even at the widest allowed width: fill the lines
    low, high = 1, limit
    while low < high:  # heightForWidth only grows as the width shrinks...
        middle = (low + high) // 2
        if label.heightForWidth(middle) > unlimited:
            low = middle + 1
        else:
            high = middle
    # ...except below the widest word: a word without a break point only
    # overflows, so its height never changes and the search ran down to 1 px
    # ("Home" became an empty toast). Never go narrower than that word.
    shown = label.text() if text is None else text
    floor = _widest_run(label.fontMetrics(), shown) + _label_extra(label)
    return max(low, min(limit, floor))


def _row_chrome(layout, skip) -> int:
    """Width of a horizontal toast row without the *skip* widget's own width."""
    margins = layout.contentsMargins()
    total, shown = margins.left() + margins.right(), 0
    for index in range(layout.count()):
        item = layout.itemAt(index)
        if item is None or item.isEmpty():
            continue
        shown += 1
        if item.widget() is not skip:
            total += item.sizeHint().width()
    return total + max(0, layout.spacing()) * max(0, shown - 1)


def _toast_area(host):
    """``(anchor, work)`` in global coordinates: the part of *host* that is on
    its screen, and that screen's work area less a small margin.

    Toasts line up with the anchor and never leave the work area, so a window
    near a screen edge or partly off screen still shows the whole toast.
    """
    from PyQt5.QtCore import QRect
    from PyQt5.QtWidgets import QApplication

    frame = QRect(host.mapToGlobal(host.rect().topLeft()), host.size())
    screen = None
    handle = host.windowHandle()
    if handle is not None:
        screen = handle.screen()
    if screen is None:
        screen = QApplication.screenAt(frame.center()) or QApplication.primaryScreen()
    work = screen.availableGeometry() if screen is not None else QRect(frame)
    work = work.adjusted(_SCREEN_MARGIN, _SCREEN_MARGIN, -_SCREEN_MARGIN, -_SCREEN_MARGIN)
    anchor = frame.intersected(work)
    if anchor.isEmpty():
        anchor = QRect(work)
    return anchor, work


def _clamped(x: int, y: int, toast, work):
    """``(x, y)`` moved just enough for *toast* to sit inside *work*."""
    x = max(work.left(), min(x, work.left() + work.width() - toast.width()))
    y = max(work.top(), min(y, work.top() + work.height() - toast.height()))
    return x, y


def _place(toast, x: int, y: int) -> None:
    if toast.x() != x or toast.y() != y:
        toast.move(x, y)


def _arrange_toasts(host, showing=None) -> None:
    """Place this window's toasts (the visible ones and *showing*, which is
    about to be shown), all kept on screen: the error toast centred at the
    bottom, and a column at the bottom right — the "Saved …" notifications,
    oldest lowest, with the activity toast on top. The column starts above
    the error toast wherever the two would overlap, so no toast ever covers
    another one's text or buttons."""
    from PyQt5.QtCore import QRect

    def shown(toast):
        return toast is not None and _alive(toast) and (toast is showing or not toast.isHidden())

    anchor, work = _toast_area(host)
    error = getattr(host, "_turboadb_error_toast", None)
    error_rect = None
    if shown(error):
        x = anchor.left() + (anchor.width() - error.width()) // 2
        y = anchor.top() + anchor.height() - _ERROR_BOTTOM - error.height()
        x, y = _clamped(x, y, error, work)
        _place(error, x, y)
        error_rect = QRect(x, y, error.width(), error.height())
    column = [note for note in getattr(host, "_turboadb_toasts", None) or ()
              if note is not None and _alive(note) and (note is showing or note.isVisible())]
    activity = getattr(host, "_turboadb_activity_toast", None)
    if shown(activity):
        column.append(activity)
    bottom = anchor.top() + anchor.height() - _ACTIVITY_BOTTOM  # the next toast ends here
    for toast in column:
        x = anchor.left() + anchor.width() - _TOAST_SIDE - toast.width()
        y = bottom - toast.height()
        if error_rect is not None and QRect(
            x, y, toast.width(), toast.height() + _TOAST_GAP
        ).intersects(error_rect):
            y = min(y, error_rect.top() - _TOAST_GAP - toast.height())
        x, y = _clamped(x, y, toast, work)
        _place(toast, x, y)
        bottom = y - _TOAST_GAP


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

    The toast is as wide as its text on one line, up to ``_ACTIVITY_TEXT_MAX``
    (less on a narrow window); only longer text wraps, and the height follows
    the wrapped lines. A warning younger than ``_WARNING_HOLD_S`` is not
    replaced by an info/OK message: that one follows once the hold is over.
    """
    import time

    from PyQt5.QtCore import Qt, QTimer
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
        toast.text.setTextFormat(Qt.PlainText)
        toast.text.setWordWrap(True)
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
        toast.level, toast.shown_at, toast.updated_at = None, 0.0, float("-inf")
        toast.pending = None
        toast.hold = QTimer(toast)
        toast.hold.setSingleShot(True)

        def show_pending(toast=toast, host=host):
            pending, toast.pending = toast.pending, None
            if pending is not None and _alive(host):
                activity_toast(host, **pending)

        toast.hold.timeout.connect(show_pending)
        host._turboadb_activity_toast = toast

    now = time.monotonic()
    visible = not toast.isHidden()
    kwargs = dict(message=message, level=level, action_text=action_text, action=action,
                  duration_ms=duration_ms)
    if (visible and level != "warning" and toast.level == "warning"
            and now - toast.shown_at < _WARNING_HOLD_S):
        # The warning stays; the newest info/OK message follows after the hold
        # (the log and the status bar already have it).
        toast.pending = kwargs
        if not toast.hold.isActive():
            toast.hold.start(max(1, int((_WARNING_HOLD_S - (now - toast.shown_at)) * 1000)))
        return toast
    toast.hold.stop()
    toast.pending = None
    has_action = bool(action_text and action is not None)
    toast.on_action = action
    unchanged = (visible and toast.message == message and toast.level == level
                 and toast.action.text() == action_text
                 and toast.action.isVisibleTo(toast) == has_action)
    if not unchanged:
        glyph, tone = {"ok": ("check", "ok"), "warning": ("alert", "warn")}.get(
            level, ("info", "accent"))
        toast.mark.setIcon(icon(glyph, tone))
        toast.message = message
        toast.action.setText(action_text)
        toast.action.setVisible(has_action)
        burst = visible and now - toast.updated_at < _BURST_S
        _fit_activity(toast, host, message, keep=toast.size() if burst else None)
        _arrange_toasts(host, showing=toast)
    if toast.level != level or not visible or level == "warning" and not unchanged:
        toast.shown_at = now
    toast.level = level
    toast.updated_at = now
    toast.show()
    toast.raise_()
    toast.timer.start(duration_ms or (8000 if level == "warning" else 4000))
    return toast


def _fit_activity(toast, host, message: str, keep=None) -> None:
    """Give the activity toast's text its one-line width (wrapping only past
    the widest allowed width), then size the whole toast around it. With
    *keep* (the current size, during a burst of updates) it only grows."""
    toast.ensurePolished()
    label, layout = toast.text, toast.layout()
    layout.invalidate()
    anchor, work = _toast_area(host)
    # The text may use the window's visible width less the side gaps and the
    # toast's icon and buttons, up to the widest one-line message.
    scale = _font_scale(label)
    chrome = _row_chrome(layout, label)
    widest = round(_ACTIVITY_TEXT_MAX * scale)
    limit = min(widest, anchor.width() - 2 * _TOAST_SIDE - chrome)
    # A very narrow window must not squeeze every word onto its own line:
    # the toast may then overhang the window, but never the screen.
    limit = max(limit, min(round(_ACTIVITY_TEXT_MIN * scale), widest))
    limit = max(40, min(limit, work.width() - chrome))
    metrics = label.fontMetrics()
    room = limit - _label_extra(label)
    shown = _breakable(message, metrics, room)
    lines = max(1, min(_ACTIVITY_LINES, (work.height() - 40) // max(1, metrics.lineSpacing())))
    shown = _shortened(shown, metrics, room, lines, "(shortened — the log has all of it)")
    label.setText(shown)
    # QLabel measures every width below its minimum at the minimum, so the
    # previous message's fixed width has to go before measuring this one.
    label.setMinimumWidth(0)
    label.setMaximumWidth(_QWIDGETSIZE_MAX)
    width = _one_line_width(label, limit)
    min_height = 0
    if keep is not None:
        width = max(width, min(limit, keep.width() - chrome))
        min_height = keep.height()
    label.setFixedWidth(width)
    _fit_toast(toast, chrome + width, min_height=min_height)


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
    """The alert sound, at most once every ``_ERROR_BEEP_GAP_S`` seconds."""
    import time

    now = time.monotonic()
    if now - _LAST_ERROR_SOUND[0] < _ERROR_BEEP_GAP_S:
        return
    _LAST_ERROR_SOUND[0] = now
    _play_error_sound()


def error_toast(parent, message: str, *, title: str = "Error", action_text: str = "Show log",
                action=None, duration_ms: int = 12000):
    """A large red toast at the bottom centre of the window, with a beep.

    Errors arriving while it is up update it in place and count them, so a
    burst of failures is one popup and one sound, not a pile of both: the
    sound plays only when the toast appears, not for errors that update it.
    A very long message (a stack trace) is shortened on screen; Copy and the
    log have all of it.
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
        copy.clicked.connect(lambda: _copy_text(toast.message))
        head.addWidget(copy)
        close = QPushButton("Dismiss")
        close.clicked.connect(toast.hide)
        head.addWidget(close)
        box.addLayout(head)
        toast.text = QLabel()
        toast.text.setObjectName("errorToastText")
        toast.text.setTextFormat(Qt.PlainText)
        toast.text.setWordWrap(True)
        toast.text.setTextInteractionFlags(Qt.TextSelectableByMouse)
        _clean_copy_menu(toast.text, lambda: toast.message)
        box.addWidget(toast.text)
        row = QHBoxLayout()
        row.addStretch(1)
        toast.action = QPushButton()
        toast.action.clicked.connect(lambda: toast.on_action and toast.on_action())
        row.addWidget(toast.action)
        box.addLayout(row)
        toast.count = 0
        host._turboadb_error_toast = toast
    # Only a dismissed or timed-out toast starts counting again from 1.
    appearing = toast.isHidden()
    toast.count = 1 if appearing else toast.count + 1
    toast.title.setText(title if toast.count == 1 else f"{title} ({toast.count})")
    toast.message = message
    toast.on_action = action
    toast.action.setText(action_text)
    toast.action.setVisible(bool(action_text and action is not None))
    toast.ensurePolished()
    anchor, work = _toast_area(host)
    scale = _font_scale(toast.text)
    width = min(round(_ERROR_WIDTH[1] * scale),
                max(round(_ERROR_WIDTH[0] * scale), anchor.width() - 160))
    # A small window: stay inside it when the buttons still fit, and on the
    # screen in any case.
    width = min(width, max(anchor.width() - 2 * _TOAST_SIDE, toast.minimumSizeHint().width()))
    width = min(width, work.width())
    margins = toast.layout().contentsMargins()
    text_width = width - margins.left() - margins.right() - _label_extra(toast.text)
    metrics = toast.text.fontMetrics()
    broken = _breakable(message, metrics, text_width)
    lines = _ERROR_LINES
    for _attempt in range(4):
        toast.text.setText(_shortened(broken, metrics, text_width, lines,
                                      "(shortened — Copy or Show log has all of it)"))
        _fit_toast(toast, width)
        if toast.height() <= work.height() or lines == 1:
            break
        # Still taller than the screen: fewer lines, so the buttons stay on it.
        excess = toast.height() - work.height()
        lines = max(1, lines - (excess + metrics.lineSpacing() - 1) // metrics.lineSpacing())
    _arrange_toasts(host, showing=toast)
    toast.show()
    toast.raise_()
    toast.timer.start(duration_ms)
    if appearing:
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
