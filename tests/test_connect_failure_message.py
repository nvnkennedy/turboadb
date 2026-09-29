"""A failed connect is reported without a modal message box.

The failure arrives from the connect worker, maybe for a tab in the
background.  QMessageBox.warning ran an event loop of its own, so every other
tab waited for it, and closing the tab or quitting TurboADB meanwhile
destroyed the box under that loop, which crashed."""

import pytest

pytest.importorskip("PyQt5")

from PyQt5.QtCore import Qt  # noqa: E402


@pytest.fixture
def boxes(monkeypatch):
    """Record the message boxes shown instead of drawing them (a message box
    can't be shown on the offscreen platform)."""
    from PyQt5.QtWidgets import QMessageBox

    shown = []
    monkeypatch.setattr(QMessageBox, "show", lambda self: shown.append(self))

    def blocked(*_args, **_kwargs):
        raise AssertionError("a modal message box would block the whole window")

    for name in ("warning", "exec_", "exec"):
        monkeypatch.setattr(QMessageBox, name, blocked, raising=False)
    return shown


def _tab():
    from turboadb.gui.device_tab import DeviceTab

    tab = DeviceTab({"name": "mock", "serial": "123"})
    tab._started_connect = True  # never a real connect
    return tab


def _pump(qapp, rounds=5):
    from PyQt5.QtCore import QCoreApplication, QEvent

    for _ in range(rounds):
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        qapp.processEvents()


def test_a_failed_connect_shows_its_message_without_blocking(qapp, boxes):
    tab = _tab()
    try:
        tab._on_fail("device offline")  # returns at once: no event loop of its own
        assert len(boxes) == 1
        box = boxes[0]
        assert box.text() == "device offline" and box.windowTitle() == "Connect failed"
        assert box.windowModality() == Qt.NonModal
        assert box.testAttribute(Qt.WA_DeleteOnClose)
        assert box.parent() is tab  # it goes with the tab
        assert not tab.btn_reconnect.isHidden()
    finally:
        tab.close_session()
        tab.close()


def test_a_newer_failure_or_a_reconnect_replaces_the_message(qapp, boxes, monkeypatch):
    from PyQt5 import sip

    tab = _tab()
    monkeypatch.setattr(tab, "start_connect", lambda: None)
    try:
        tab._on_fail("first")
        tab._on_fail("second")
        _pump(qapp)
        assert sip.isdeleted(boxes[0])  # one message per tab, the newest
        assert boxes[1].text() == "second"
        tab.reconnect_device()
        _pump(qapp)
        assert sip.isdeleted(boxes[1])  # trying again: the old message goes
    finally:
        tab.close_session()
        tab.close()


def test_a_connect_that_works_after_all_takes_the_message_away(qapp, boxes):
    from PyQt5 import sip

    tab = _tab()
    try:
        tab._on_fail("device offline")
        tab._terminal_only = True  # the smallest connected tab: just a terminal
        tab._on_connected(type("Handler", (), {"serial": "123", "config": None})(), None)
        _pump(qapp)
        assert sip.isdeleted(boxes[0])
    finally:
        tab.close_session()
        tab.close()


def test_closing_the_tab_while_the_message_is_open_is_safe(qapp, boxes):
    from PyQt5 import sip

    tab = _tab()
    tab._on_fail("device offline")
    box = boxes[0]
    tab.close_session()  # what quitting does to every tab
    tab.close()
    tab.deleteLater()
    _pump(qapp)
    assert sip.isdeleted(box)
