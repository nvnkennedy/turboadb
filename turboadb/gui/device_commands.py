"""Ordered, bounded execution of short GUI-originated device commands."""

from __future__ import annotations

import logging
import queue

from PyQt5.QtCore import QThread, pyqtSignal

from ..results import OperationResult

_log = logging.getLogger(__name__)

# The first Android whose `input` takes a display (-d): Android 10.
INPUT_DISPLAY_SDK = 29


def input_display(display_id, displays, sdk=None):
    """The display a key or typed text for *display_id* is sent with, or None
    for the plain command.

    Android sends a key without a display to whichever display has the focus.
    On a device with several (*displays* is how many) that need not be display
    0: Home pressed for the centre screen went to the passenger screen someone
    had just touched.  So display 0 is named there, like any other display.
    It keeps the plain command on a device with one display, and on one whose
    ``input`` takes no display: an Android before 10, or one whose version
    (*sdk*, its API level) is not known."""
    try:
        display_id = int(display_id)
    except (TypeError, ValueError):
        display_id = 0
    if display_id:
        return display_id
    if displays > 1 and sdk is not None and sdk >= INPUT_DISPLAY_SDK:
        return 0
    return None


def command_error(result, rejected: str):
    """Failure text for a device command's result, or None when it worked.

    The one rule every screen control reports by (the keyboard, the screen's
    taps and keys, a display's buttons): a safe-mode result that failed gives
    its error, or *rejected* when it carries none, and a command that returned
    False (the device refused it) gives *rejected*."""
    if isinstance(result, OperationResult):
        if not result.success:
            return str(result.error or rejected)
        result = result.value
    return rejected if result is False else None


class DeviceCommandDispatcher(QThread):
    """Run short ADB control operations in a deterministic order.

    Screen streaming, file transfers, and recording deliberately do not use this
    queue: they may run for seconds or minutes.  Keyboard, tap/swipe, and control
    panel actions do use it, so rapid UI input cannot create an unbounded number
    of threads or overtake another command of the same priority.
    """

    delivered = pyqtSignal(object, object, object)

    def __init__(self, max_pending: int = 256):
        super().__init__()
        self._queue = queue.Queue(maxsize=max(1, int(max_pending)))
        self._stopping = False
        # Accepted submissions so far.  A caller that still holds an unstarted
        # command can merge more work into it only while this is unchanged,
        # i.e. while nothing else has been queued behind that command.
        self.submitted = 0
        self.delivered.connect(self._deliver)

    def submit(self, fn, *, on_done=None, on_fail=None, priority: int = 10) -> bool:
        """Queue ``fn()``; ``on_done(result)`` / ``on_fail(message)`` run on the UI thread.

        ``priority`` is accepted for call-site compatibility but IGNORED: every
        command runs in strict submission (FIFO) order.  Returns False (after
        calling ``on_fail``) when the queue is closing or full.
        """
        if self._stopping:
            if on_fail:
                on_fail("device command queue is closing")
            return False
        if not self.isRunning():
            self.start()
        # ``priority`` remains part of the public call shape for compatibility,
        # but strict FIFO is intentional: a later key, tap, or control must not
        # overtake an earlier device action.
        _ = priority
        item = (fn, on_done, on_fail)
        try:
            self._queue.put_nowait(item)
            self.submitted += 1
            return True
        except queue.Full:
            if on_fail:
                on_fail("device command queue is full; wait for the device to catch up")
            return False

    def stop(self) -> None:
        self._stopping = True
        try:
            self._queue.put_nowait((None, None, None))
        except queue.Full:
            # Make room for the stop marker. Pending UI commands are no longer
            # useful once the owning device tab is closing.
            try:
                self._queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._queue.put_nowait((None, None, None))
            except queue.Full:
                pass

    def run(self) -> None:
        while True:
            fn, on_done, on_fail = self._queue.get()
            if fn is None or self._stopping:
                break
            try:
                result = fn()
            except Exception as exc:
                if not self._stopping:
                    self.delivered.emit(on_fail, None, f"{type(exc).__name__}: {exc}")
            else:
                if not self._stopping:
                    self.delivered.emit(on_done, result, None)

    @staticmethod
    def _deliver(callback, result, error) -> None:
        if callback is None:
            return
        try:
            callback(error if error is not None else result)
        except RuntimeError as exc:
            # The widget that queued the command was closed (its C++ object is
            # gone) before the result arrived — nothing left to update.
            _log.debug("device command callback skipped: %s", exc)
        except Exception:
            # A faulty result handler must not escape into Qt's event loop
            # (the global excepthook turns that into a modal error popup).
            _log.exception("device command callback failed")
