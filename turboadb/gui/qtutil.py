"""Small Qt helpers shared by the GUI panels."""

from __future__ import annotations

# Threads parked here stay referenced until they finish, so their QThread C++
# object is never destroyed mid-run (which hard-crashes Qt with
# "QThread: Destroyed while thread is still running").
_parked = set()


def _unpark(t) -> None:
    _parked.discard(t)


def park_thread(t) -> None:
    """Keep *t* (a QThread) alive until it finishes even after the widget that
    owns it is closed — e.g. a device tab closed while its connect thread is
    still waiting on a slow remote adb server. Safe to call before start():
    the reference is simply dropped once the thread finishes."""
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
    t.finished.connect(lambda: _unpark(t))
