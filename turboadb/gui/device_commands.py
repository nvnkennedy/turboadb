"""Ordered, bounded execution of short GUI-originated device commands."""

from __future__ import annotations

import logging
import queue

from PyQt5.QtCore import QThread, pyqtSignal

_log = logging.getLogger(__name__)


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
