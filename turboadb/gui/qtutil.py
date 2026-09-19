"""Small Qt helpers shared by the GUI panels."""

from __future__ import annotations

from PyQt5.QtCore import (
    QEvent, QEasingCurve, QPoint, QPropertyAnimation, QRect, Qt, QThread, QTimer, pyqtSignal,
)
from PyQt5.QtWidgets import QFrame, QTabWidget

# Threads parked here stay referenced until they finish, so their QThread C++
# object is never destroyed mid-run (which hard-crashes Qt with
# "QThread: Destroyed while thread is still running").
_parked = set()


class AnimatedTabWidget(QTabWidget):
    """A native Qt tab widget with a small, safe animated tab indicator.

    It uses native ``QPropertyAnimation`` for an active-tab underline and a
    16-pixel slide-in of the newly selected page. No page is composited or
    duplicated — effects on nested pages previously caused Windows to paint a
    device twice and corrupt the second terminal's rendering.
    """

    def __init__(self, parent=None, *, transition_ms: int = 145):
        super().__init__(parent)
        self._transition_ms = max(0, int(transition_ms))
        self._tab_indicator = QFrame(self.tabBar())
        self._tab_indicator.setObjectName("animatedTabIndicator")
        self._tab_indicator.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self._tab_indicator.hide()
        self._indicator_animation = QPropertyAnimation(
            self._tab_indicator, b"geometry", self
        )
        self._indicator_animation.setDuration(self._transition_ms)
        self._indicator_animation.setEasingCurve(QEasingCurve.OutCubic)
        self._page_animation = QPropertyAnimation(self)
        self._page_animation.setDuration(self._transition_ms)
        self._page_animation.setEasingCurve(QEasingCurve.OutCubic)
        self._animated_page = None
        self._animated_page_target = None
        self._previous_index = -1
        # Deferred work runs from timers owned by this widget, so nothing can
        # fire after it is deleted: a QTimer.singleShot lambda on a deleted
        # wrapper raised RuntimeError, which PyQt turns into a process abort.
        self._pending_indicator_index = -1
        self._pending_page = None
        self._sync_timer = self._deferred(self._sync_tab_indicator)
        self._indicator_timer = self._deferred(
            lambda: self._start_indicator_animation(self._pending_indicator_index)
        )
        self._page_timer = self._deferred(self._run_pending_page_animation)
        self.currentChanged.connect(self._animate_tab_indicator)
        self.currentChanged.connect(self._animate_page_entry)
        self.tabBar().installEventFilter(self)

    def _deferred(self, slot):
        """A zero-delay single-shot timer parented to (and deleted with) this widget."""
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.setInterval(0)
        timer.timeout.connect(slot)
        return timer

    def eventFilter(self, watched, event):
        if watched is self.tabBar() and event.type() in (
            QEvent.Resize,
            QEvent.Show,
            QEvent.LayoutRequest,
        ):
            self._sync_timer.start()
        return super().eventFilter(watched, event)

    def _indicator_rect(self, index=None):
        if index is None:
            index = self.currentIndex()
        if index < 0 or index >= self.count():
            return None
        rect = self.tabBar().tabRect(index)
        if not rect.isValid() or rect.width() < 1:
            return None
        height = 3
        return QRect(rect.left(), max(rect.top(), rect.bottom() - height + 1), rect.width(), height)

    def _sync_tab_indicator(self):
        """Place the underline immediately after a resize/layout change."""
        rect = self._indicator_rect()
        if rect is None:
            self._tab_indicator.hide()
            return
        self._indicator_animation.stop()
        self._tab_indicator.setGeometry(rect)
        self._tab_indicator.show()

    def _animate_tab_indicator(self, index):
        """Slide the active underline; never animate or reparent the page."""
        self._pending_indicator_index = index
        self._indicator_timer.start()

    def _start_indicator_animation(self, index):
        rect = self._indicator_rect(index)
        if rect is None:
            self._tab_indicator.hide()
            return
        if not self._tab_indicator.isVisible() or self._transition_ms == 0:
            self._sync_tab_indicator()
            return
        start = self._tab_indicator.geometry()
        if start == rect:
            return
        self._indicator_animation.stop()
        self._indicator_animation.setStartValue(start)
        self._indicator_animation.setEndValue(rect)
        self._tab_indicator.show()
        self._indicator_animation.start()

    def _animate_page_entry(self, index):
        """Give the new page a short native slide-in without compositing pages."""
        previous = self._previous_index
        self._previous_index = index
        if previous < 0 or self._transition_ms == 0:
            return
        direction = 1 if index > previous else -1
        self._pending_page = (index, direction)
        self._page_timer.start()

    def _run_pending_page_animation(self):
        pending, self._pending_page = self._pending_page, None
        if pending is not None:
            self._start_page_animation(*pending)

    def _start_page_animation(self, index, direction):
        page = self.widget(index)
        if page is None or not page.isVisible():
            return
        if self._animated_page is not None and self._animated_page_target is not None:
            try:
                self._animated_page.move(self._animated_page_target)
            except RuntimeError:
                pass
        self._page_animation.stop()
        target = page.pos()
        start = target + QPoint(16 * direction, 0)
        page.move(start)
        self._animated_page = page
        self._animated_page_target = target
        self._page_animation.setTargetObject(page)
        self._page_animation.setPropertyName(b"pos")
        self._page_animation.setStartValue(start)
        self._page_animation.setEndValue(target)
        self._page_animation.start()


def _unpark(t) -> None:
    _parked.discard(t)


def park_thread(t) -> None:
    """Keep *t* (a QThread) alive until Qt has deleted it, even after the widget
    that owns it is closed — e.g. a device tab closed while its connect thread
    is still waiting on a slow remote adb server. Safe to call before start().

    The reference is held until ``destroyed``, not ``finished``: a QThread that
    Python still owns when it asks for ``deleteLater`` can be garbage-collected
    (which deletes the C++ object) while that delete is still queued, and Qt
    then carries the queued delete out on freed memory — a native crash with no
    Python traceback, in whatever ran next.
    """
    if t is None or t in _parked:
        return
    try:
        finished = t.isFinished()
    except RuntimeError:  # the C++ object is already gone
        return
    if finished:
        return
    _parked.add(t)
    t.finished.connect(t.deleteLater)
    t.destroyed.connect(lambda *_args: _unpark(t))


def thread_running(t) -> bool:
    """``t.isRunning()`` that treats a missing or already-deleted QThread as stopped.

    Threads connected to ``finished -> deleteLater`` leave a Python wrapper whose
    C++ object is gone; calling ``isRunning()`` on it raises ``RuntimeError``.
    """
    if t is None:
        return False
    try:
        return t.isRunning()
    except RuntimeError:
        return False


class FunctionThread(QThread):
    """Run ``fn()`` off the UI thread and report its result or error by signal."""

    done = pyqtSignal(object)
    fail = pyqtSignal(str)

    def __init__(self, fn):
        super().__init__()
        self.fn = fn

    def run(self):
        try:
            result = self.fn()
        except Exception as exc:
            self.fail.emit(f"{type(exc).__name__}: {exc}")
        else:
            self.done.emit(result)


# ---- panel job helpers (append-only section) -------------------------------


def run_job(jobs, fn, on_done=None, on_fail=None):
    """Run ``fn()`` on a :class:`FunctionThread` tracked in the *jobs* list.

    ``on_done(result)`` / ``on_fail(message)`` are delivered on the UI thread.
    The job removes itself from *jobs* and schedules its own deletion when it
    finishes, so owners never keep stale wrappers.  Pair with
    :func:`close_jobs` when the owning widget closes.
    """
    job = FunctionThread(fn)
    if on_done is not None:
        job.done.connect(on_done)
    if on_fail is not None:
        job.fail.connect(on_fail)

    def _forget(j=job):
        try:
            jobs.remove(j)
        except ValueError:
            pass

    job.finished.connect(_forget)
    # Parking before start() keeps the wrapper referenced even if the owner
    # drops *jobs*; park_thread also schedules deleteLater on finish.
    jobs.append(job)
    park_thread(job)
    job.start()
    return job


def unwrap(res):
    """Turn a safe-mode/raw handler result into a plain value or raise.

    Shared by every panel that calls the engine from a worker thread, so one
    result convention is decoded in one place.
    """
    from ..results import CommandResult, OperationResult

    if isinstance(res, OperationResult):
        if not res.success:
            raise res.error or RuntimeError(f"{res.action or 'ADB operation'} failed")
        res = res.value
    if isinstance(res, CommandResult):
        if not res.ok:
            raise RuntimeError(res.stderr or "ADB command failed")
        res = res.text
    if res is False:
        raise RuntimeError("device rejected the command")
    return res


_ICON_CACHE = {}


def cached_icon(name, tone=None):
    """One shared ``QIcon`` per ``(name, tone)``.

    Icons read their colour from the palette at paint time, so a single
    instance still follows live theme switches — a fresh icon per table row or
    list item only wasted memory.
    """
    key = (name, tone)
    cached = _ICON_CACHE.get(key)
    if cached is None:
        from .icons import icon

        cached = _ICON_CACHE[key] = icon(name, tone)
    return cached


def page_toolbar(hspacing: int = 8, vspacing: int = 6, margins=(12, 8, 12, 8)):
    """``(widget, layout)`` for a page's top toolbar row.

    The layout is a :class:`flowlayout.ToolbarFlowLayout`, so a narrow tab wraps
    the row instead of widening the whole window (see ARCHITECTURE.md).
    """
    from PyQt5.QtWidgets import QWidget

    from .flowlayout import ToolbarFlowLayout

    toolbar = QWidget()
    toolbar.setObjectName("pageToolbar")
    toolbar.setAttribute(Qt.WA_StyledBackground, True)
    row = ToolbarFlowLayout(toolbar, hspacing=hspacing, vspacing=vspacing)
    row.setContentsMargins(*margins)
    return toolbar, row


def disconnect_signals(obj, names=("done", "fail")) -> None:
    """Disconnect every slot from the named signals of *obj* (missing/deleted ok)."""
    if obj is None:
        return
    for name in names:
        try:
            getattr(obj, name).disconnect()
        except (AttributeError, TypeError, RuntimeError):
            pass


def close_jobs(jobs, names=("done", "fail")) -> None:
    """Detach and park every job in *jobs* without blocking the UI thread.

    Result signals are disconnected first, so a job finishing after its owner
    closed can never call back into a deleted widget.  Running threads stay
    referenced (``park_thread``) until they finish; nothing waits here.
    """
    for job in list(jobs):
        disconnect_signals(job, names)
        park_thread(job)
    del jobs[:]
