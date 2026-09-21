"""Another terminal session for an open device, one click away.

The extra terminal tab already existed but was reachable only by opening the
same device a second time. Headless; the fake device from test_adb_processes
stands in for adb."""

import types

import pytest

pytest.importorskip("PyQt5")

from test_adb_processes import _Device, _close, _connect, _tab  # noqa: E402
from turboadb.gui import device_tab as dt  # noqa: E402


@pytest.fixture
def tab(qapp):
    widget = _tab(qapp)
    _connect(widget, _Device())
    widget.show()
    qapp.processEvents()
    yield widget
    _close(qapp, widget)


def _right_click(tab, widget, monkeypatch):
    """The labels of the menu a right-click on *widget*'s tab shows (None when
    there is no menu), and that menu's actions."""
    shown = []
    monkeypatch.setattr(dt.QMenu, "exec_", lambda menu, *a: shown.append(menu.actions()))
    bar = tab.inner.tabBar()
    tab._tab_context_menu(bar.tabRect(tab.inner.indexOf(widget)).center())
    if not shown:
        return None, []
    return [action.text() for action in shown[0]], shown[0]


def _more_action(tab, text):
    return next((a for a in tab.btn_more.menu().actions() if a.text() == text), None)


def test_right_click_on_the_terminal_tab_offers_a_new_session(tab, monkeypatch):
    asked = []
    tab.terminal_session_requested.connect(lambda: asked.append(True))
    labels, actions = _right_click(tab, tab.shell, monkeypatch)
    assert labels == ["New terminal session"]
    actions[0].trigger()
    assert asked == [True]


def test_more_menu_offers_a_new_terminal_session(tab):
    asked = []
    tab.terminal_session_requested.connect(lambda: asked.append(True))
    action = _more_action(tab, "New terminal session")
    assert action is not None and "Android shell" in action.toolTip()
    action.trigger()
    assert asked == [True]


def test_a_terminal_only_tab_offers_it_on_its_terminal_tab(qapp, monkeypatch):
    """Its header has no More menu, so the tab's right-click is the way in."""
    widget = _tab(qapp, terminal_only=True)
    try:
        _connect(widget, _Device())
        assert widget.btn_more.isHidden()
        asked = []
        widget.terminal_session_requested.connect(lambda: asked.append(True))
        labels, actions = _right_click(widget, widget.shell, monkeypatch)
        assert labels == ["New terminal session"]
        actions[0].trigger()
        assert asked == [True]
    finally:
        _close(qapp, widget)


def test_a_closed_tab_asks_for_nothing(qapp):
    widget = _tab(qapp)
    _connect(widget, _Device())
    asked = []
    widget.terminal_session_requested.connect(lambda: asked.append(True))
    _close(qapp, widget)
    widget.request_terminal_session()
    assert asked == []


def test_files_tab_menus_are_unchanged(tab, monkeypatch):
    labels, _actions = _right_click(tab, tab._lazy_pages["files"], monkeypatch)
    assert labels == ["New Files tab here"]
    assert _right_click(tab, tab.combo_view, monkeypatch)[0] is None


# --------------------------------------------------------------------------- #
# the main window opens the existing terminal-only tab
# --------------------------------------------------------------------------- #
def _stand_in_tab_class():
    from PyQt5.QtCore import pyqtSignal
    from PyQt5.QtWidgets import QWidget

    class Tab(QWidget):
        log = pyqtSignal(str)
        trace = pyqtSignal(str)
        title_changed = pyqtSignal(str)
        screen_active = pyqtSignal(bool)
        connected = pyqtSignal()
        terminal_session_requested = pyqtSignal()

        def __init__(self, session, parent=None, *, terminal_only=False):
            super().__init__(parent)
            self.session = session
            self._terminal_only = terminal_only
            self.handler = None

        def start_connect(self):
            pass

    return Tab


def test_the_request_opens_a_terminal_only_tab_for_the_same_device(qapp, monkeypatch):
    from PyQt5.QtWidgets import QTabWidget

    import turboadb.gui.main_window as mw_mod
    from turboadb.gui.main_window import MainWindow

    monkeypatch.setattr(mw_mod, "DeviceTab", _stand_in_tab_class())
    fake = types.SimpleNamespace(
        tabs=QTabWidget(), _log=lambda *_a: None, _log_trace=lambda *_a: None,
        _on_screen_active=lambda _active: None, _set_tab_title=lambda *_a: None,
        _update_center=lambda: None, _watch_connection=lambda _tab: None,
    )
    fake._add_device_tab = lambda *a, **k: MainWindow._add_device_tab(fake, *a, **k)
    fake._open_terminal_session = (
        lambda tab, name: MainWindow._open_terminal_session(fake, tab, name))
    session = {"name": "Pixel", "type": "usb", "serial": "S1"}
    try:
        fake._add_device_tab(session, "Pixel")
        full = fake.tabs.widget(0)
        full.terminal_session_requested.emit()
        assert fake.tabs.count() == 2
        extra = fake.tabs.widget(1)
        assert extra._terminal_only
        assert fake.tabs.tabText(1) == "Pixel · Terminal"
        assert extra.session == session and extra.session is not session  # a copy
        assert fake.tabs.currentWidget() is extra
        extra.terminal_session_requested.emit()  # and another, from the extra one
        assert fake.tabs.count() == 3 and fake.tabs.widget(2)._terminal_only
    finally:
        fake.tabs.deleteLater()
        qapp.processEvents()
