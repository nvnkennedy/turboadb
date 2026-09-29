"""A screen window the exit leaves open keeps its device connection.

With Settings -> Startup "Close ADB and scrcpy when TurboADB closes" unticked,
the exit leaves separate scrcpy windows (and the adb server) running.  Closing
the device tabs then still dropped the network connection TurboADB had made
(``adb disconnect host:port``), so the window it had just kept lost its head
unit a moment later: the setting only worked for USB devices.
"""
import threading
import time
import types

import pytest

pytest.importorskip("PyQt5")

from turboadb.config import ADBConfig  # noqa: E402

NET = {"name": "head unit", "type": "network", "host": "10.0.0.5", "port": 5555}


class _NetHandler:
    def __init__(self):
        self.config = ADBConfig(serial="10.0.0.5:5555")
        self.serial = "10.0.0.5:5555"
        self.owns_connection = True  # TurboADB's `adb connect` made it
        self.disconnects = []
        self.done = threading.Event()

    def disconnect(self, safe=None):
        self.disconnects.append(threading.current_thread())
        self.done.set()
        return True


class _Session:
    running = True

    def __init__(self):
        self.stopped = False

    def stop(self, timeout=5.0):
        self.stopped = True


@pytest.fixture
def tab_with_screen(qapp, monkeypatch):
    import turboadb.devices as devices
    import turboadb.tools as tools
    from turboadb.gui import device_tab
    from turboadb.gui.device_tab import DeviceTab
    from turboadb.gui.mirror_panel import MirrorPanel

    monkeypatch.setattr(devices, "server_is_shared", lambda *_a, **_k: False)
    monkeypatch.setattr(tools, "is_adb_server_alive", lambda *_a, **_k: True)
    monkeypatch.setattr(device_tab, "_CONNECTIONS", device_tab._Connections())
    handler = _NetHandler()
    tab = DeviceTab(dict(NET))
    tab._started_connect = True
    tab._on_connected(handler, {"_probe_pending": True})
    panel = MirrorPanel(types.SimpleNamespace(serial="10.0.0.5:5555", config=None), None)
    panel.setParent(tab)
    session = _Session()
    panel._scrcpy, panel._embed_on = session, False  # a separate scrcpy window
    yield tab, panel, handler, session
    panel.close_panel()
    tab.handler = None
    tab.close()
    tab.deleteLater()
    qapp.processEvents()


def test_a_kept_window_keeps_the_connection_it_uses(tab_with_screen):
    tab, panel, handler, session = tab_with_screen
    assert panel.keep_window_open()  # the exit, with the setting off
    tab.close_session()
    assert not handler.done.wait(0.5)
    assert handler.disconnects == [] and not session.stopped


def test_without_a_kept_window_the_connection_is_still_dropped(tab_with_screen, qapp):
    tab, panel, handler, _session = tab_with_screen
    panel._scrcpy = None  # nothing kept: the setting is on, or no separate window
    tab.close_session()
    deadline = time.monotonic() + 5
    while not handler.done.is_set() and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.01)
    assert len(handler.disconnects) == 1
