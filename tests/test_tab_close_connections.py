"""Closing a device tab lets go of its network connection instead of cutting it.

Closing a tab used to run ``adb disconnect host:port`` for every network
target, on the UI thread when no worker was pending: it disconnected the
device from another tab on it (a terminal-only session), from other tools
(Android Studio's wireless debugging) and, through a remote adb server, from
every user of that server; and against a server that stopped answering it froze
the window.  Now only a connection this app made is dropped, once the last open
tab using it closes, never on a remote or shared server, and never on the UI
thread.
"""
import threading
import time

import pytest

pytest.importorskip("PyQt5")

from turboadb.config import ADBConfig  # noqa: E402

try:  # conftest stubs _ConnectThread.run for every test; keep the real one
    from turboadb.gui.device_tab import _ConnectThread as _RealConnectThread

    _REAL_CONNECT_RUN = _RealConnectThread.run
except Exception:  # pragma: no cover - PyQt5 missing: the module is skipped
    _REAL_CONNECT_RUN = None

NET = {"name": "head unit", "type": "network", "host": "10.0.0.5", "port": 5555}


class _NetHandler:
    """A connected handler for 10.0.0.5:5555 (or *target*) that records disconnects."""

    def __init__(self, target="10.0.0.5:5555", *, owns=True, server=None, slow=0.0):
        self.config = ADBConfig(serial=target, adb_server_host=server)
        self.serial = target
        self.owns_connection = owns
        self.slow = slow
        self.disconnects = []  # the thread each disconnect ran on
        self.done = threading.Event()

    def disconnect(self, safe=None):
        time.sleep(self.slow)
        self.disconnects.append(threading.current_thread())
        self.done.set()
        return True


@pytest.fixture(autouse=True)
def not_shared(monkeypatch):
    """This PC's adb server runs and is not shared with other machines (no
    socket or LAN probe), and no tab of another test counts as a user of these
    made-up targets."""
    import turboadb.devices as devices
    import turboadb.tools as tools
    from turboadb.gui import device_tab

    monkeypatch.setattr(devices, "server_is_shared", lambda *_a, **_k: False)
    # a disconnect runs only while the local server answers (see
    # test_exit_disconnect.py)
    monkeypatch.setattr(tools, "is_adb_server_alive", lambda *_a, **_k: True)
    monkeypatch.setattr(device_tab, "_CONNECTIONS", device_tab._Connections())


def _connecting(tab):
    """Start *tab*'s connect without running it (the worker is "still waiting")."""
    tab._started_connect = False
    tab._ct.start = lambda: None
    tab.start_connect()
    return tab


@pytest.fixture
def tabs(qapp):
    from turboadb.gui.device_tab import DeviceTab

    made = []

    def make(session=NET, handler=None, **kwargs):
        tab = DeviceTab(dict(session), **kwargs)
        tab._started_connect = True  # never the real connect
        if handler is not None:
            # what the connect worker delivers once its probe has the identity
            tab._on_connected(handler, {"_probe_pending": True})
        made.append(tab)
        return tab

    yield make
    for tab in made:
        tab.handler = None
        tab.close_session()
        tab.close()
        tab.deleteLater()
    qapp.processEvents()


def _never(handler, wait=0.3):
    """True when *handler* was not disconnected within *wait* seconds."""
    return not handler.done.wait(wait) and handler.disconnects == []


def _pump(qapp, until, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qapp.processEvents()
        if until():
            return True
        time.sleep(0.01)
    qapp.processEvents()
    return until()


def test_a_connection_the_tab_made_is_dropped_off_the_ui_thread(tabs):
    handler = _NetHandler(owns=True, slow=1.0)
    tab = tabs(handler=handler)
    started = time.monotonic()
    tab.close_session()
    assert time.monotonic() - started < 0.5  # the slow `adb disconnect` never blocks the UI
    assert handler.done.wait(5)
    assert len(handler.disconnects) == 1
    assert handler.disconnects[0] is not threading.main_thread()


def test_the_drop_waits_for_the_workers_still_using_the_connection(qapp, tabs):
    from turboadb.gui.qtutil import run_job

    handler = _NetHandler(owns=True)
    tab = tabs(handler=handler)
    release = threading.Event()
    run_job(tab._threads, lambda: release.wait(10))  # e.g. a listing still running
    tab.close_session()
    _pump(qapp, lambda: False, timeout=0.3)
    assert handler.disconnects == []
    release.set()
    assert _pump(qapp, handler.done.is_set)
    assert handler.disconnects[0] is not threading.main_thread()


def test_a_connection_someone_else_made_stays_connected(tabs):
    handler = _NetHandler(owns=False)  # adb said "already connected"
    tab = tabs(handler=handler)
    tab.close_session()
    assert _never(handler)


def test_closing_one_of_two_tabs_on_a_device_keeps_it_for_the_other(tabs):
    """A full tab and its terminal-only session share 10.0.0.5:5555."""
    owner = _NetHandler(owns=True)  # the full tab's `adb connect` made it
    second = _NetHandler(owns=False)  # the terminal tab found it connected
    full = tabs(handler=owner)
    terminal = tabs(handler=second, terminal_only=True)

    full.close_session()
    assert _never(owner) and _never(second, 0.0)  # the terminal tab still uses it

    terminal.close_session()  # the last user: the connection this app made goes
    assert owner.done.wait(5) or second.done.wait(5)
    assert len(owner.disconnects) + len(second.disconnects) == 1


def test_the_other_tab_closing_first_also_leaves_it_connected(tabs):
    owner = _NetHandler(owns=True)
    second = _NetHandler(owns=False)
    full = tabs(handler=owner)
    terminal = tabs(handler=second, terminal_only=True)
    terminal.close_session()
    assert _never(owner) and _never(second, 0.0)
    full.close_session()
    assert owner.done.wait(5)


def test_a_tab_still_connecting_counts_as_a_user(tabs):
    owner = _NetHandler(owns=True)
    full = tabs(handler=owner)
    _connecting(tabs())
    full.close_session()
    assert _never(owner)


def test_a_remote_servers_connection_is_never_dropped(tabs):
    """Through a shared remote adb server, `adb disconnect` would
    disconnect the device for every user of that server."""
    session = {"name": "far", "type": "remote", "adb_host": "10.9.9.9",
               "serial": "10.0.0.5:5555"}
    handler = _NetHandler(owns=True, server="10.9.9.9")
    tab = tabs(session, handler=handler)
    tab.close_session()
    assert _never(handler)


def test_a_server_shared_with_other_machines_keeps_the_connection(tabs, monkeypatch):
    import turboadb.devices as devices

    asked = threading.Event()
    monkeypatch.setattr(devices, "server_is_shared",
                        lambda *_a, **_k: asked.set() or True)
    handler = _NetHandler(owns=True)
    tab = tabs(handler=handler)
    tab.close_session()
    assert asked.wait(5)
    assert _never(handler)


def test_a_usb_tab_runs_no_adb_on_close(tabs):
    handler = _NetHandler("R58M123", owns=False)
    tab = tabs({"name": "phone", "serial": "R58M123"}, handler=handler)
    tab.close_session()
    assert _never(handler)


# --------------------------------------------------------------------------- #
# handlers no tab took: a late connect result, a cancelled connect
# --------------------------------------------------------------------------- #
def test_a_late_connect_result_is_released_not_disconnected(tabs):
    """A result that arrives after the tab closed, or after a retry already
    connected, is let go: dropped only when it made the connection and no
    open tab uses it."""
    current = _NetHandler(owns=False)
    tab = tabs(handler=current)
    late = _NetHandler(owns=True)
    tab._on_connected(late, None)  # a retry already connected: released
    assert _never(late)  # the tab still uses 10.0.0.5:5555
    tab.close_session()  # ...until it closes: the connection the late one made goes
    assert late.done.wait(5) or current.done.wait(5)


def test_a_result_after_close_with_nobody_on_the_device_is_dropped(tabs):
    tab = tabs()
    tab.close_session()
    late = _NetHandler(owns=True)
    tab._on_connected(late, None)
    assert late.done.wait(5)
    assert tab.handler is None


def _run_connect(worker, handler):
    worker._make_handler = lambda: handler
    _REAL_CONNECT_RUN(worker)


def test_a_connect_cancelled_after_it_connected_releases_what_it_made(qapp):
    from turboadb.gui.device_tab import _ConnectThread

    worker = _ConnectThread(ADBConfig(host="10.0.0.5"), fetch_identity=False)
    handler = _NetHandler(owns=True)
    handler.connect = lambda **_kw: worker.cancel() or handler  # the tab closed meanwhile
    _run_connect(worker, handler)
    assert handler.done.wait(5)

    worker = _ConnectThread(ADBConfig(host="10.0.0.5"), fetch_identity=False)
    handler = _NetHandler(owns=False)
    handler.connect = lambda **_kw: worker.cancel() or handler
    _run_connect(worker, handler)
    assert _never(handler)


def test_a_cancelled_connect_racing_the_tab_close_still_drops_its_connection(tabs):
    """The connect's `adb connect` made the connection, then the tab closed while
    it waited. Whichever of the two lets go last drops it: here the worker
    first (the tab still counted as a user), then the tab."""
    from turboadb.gui.device_tab import _release_connection

    tab = _connecting(tabs())  # the tab uses 10.0.0.5:5555 while it connects
    made = _NetHandler(owns=True)
    _release_connection(made)  # the cancelled worker lets go first
    assert _never(made)
    tab.close_session()  # the tab never got a handler, yet the connection goes
    assert made.done.wait(5)
