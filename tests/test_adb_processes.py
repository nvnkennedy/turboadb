"""How many adb processes a device tab starts, and when.

A device tab used to fork a persistent ``adb shell`` as soon as it connected
(even while hidden), five short probe processes (three of them at once) and
build every section page up front.  These tests pin the lean behaviour: one
connect-time probe process, persistent processes only for pages that are on
screen (or started), no overlapping polls, a cap on concurrent background
commands, and nothing left running after the tab closes.
"""

import subprocess
import threading
import time

import pytest

pytest.importorskip("PyQt5")

from turboadb.core import ADBHandler  # noqa: E402
from turboadb.results import CommandResult, OperationResult  # noqa: E402


def _pump(qapp, until, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qapp.processEvents()
        if until():
            return True
        time.sleep(0.01)
    qapp.processEvents()
    return until()


# --------------------------------------------------------------------------- #
# A fake device: every adb child it "starts" is recorded and can be inspected.
# --------------------------------------------------------------------------- #
class _Stream:
    """stdout of a fake adb child: *data* then EOF, or (data None) nothing
    until the process is killed."""

    def __init__(self, proc, data):
        self._proc = proc
        self._data = data
        self.closed = False

    def read(self, _size=-1):
        if self._data is None:
            self._proc.killed.wait(10)
            return b""
        chunk, self._data = self._data, b""
        if not chunk:
            self._proc.exited.set()
        return chunk

    def close(self):
        self.closed = True


class _Proc:
    def __init__(self, argv, data):
        self.argv = list(argv)
        self.killed = threading.Event()
        self.exited = threading.Event()
        self.stdout = _Stream(self, data)

    @property
    def alive(self):
        return not self.exited.is_set()

    def poll(self):
        return None if self.alive else 0

    def wait(self, timeout=None):
        if not self.exited.wait(timeout):
            raise subprocess.TimeoutExpired(self.argv, timeout)
        return 0

    def kill(self):
        self.killed.set()
        self.exited.set()

    terminate = kill


class _Session:
    """ShellSession stand-in: an idle interactive ``adb shell``."""

    def __init__(self):
        self.argv = ["shell"]
        self.closed = threading.Event()

    @property
    def alive(self):
        return not self.closed.is_set()

    running = alive

    def read(self, _size=65536):
        time.sleep(0.005)
        return b""

    def send(self, _data):
        return True

    def close(self):
        self.closed.set()


def _probe_output(*, prompt="2000|shell|V2318", characteristics="default",
                  features=("android.hardware.telephony",), wm="Physical size: 1080x2400",
                  displays='DisplayInfo{"Built-in Screen, displayId 0", real 1080 x 2400, x}'):
    from turboadb.gui.device_tab import _DeviceProbe

    props = {
        "ro.product.manufacturer": "vivo", "ro.product.model": "V2318",
        "ro.build.version.release": "16", "ro.build.version.sdk": "36",
        "ro.product.device": "V2318", "ro.product.cpu.abi": "arm64-v8a",
        "ro.product.brand": "vivo", "ro.product.name": "V2318T",
        "ro.build.display.id": "build-1", "ro.serialno": "FAKE123",
        "ro.build.characteristics": characteristics,
    }
    mark = _DeviceProbe.MARK.format
    lines = [mark("prompt"), prompt, mark("props")]
    lines += [f"[{key}]: [{props[key]}]" for key in _DeviceProbe.PROPS]
    lines += [mark("kind")] + [f"feature:{f}" for f in features]
    lines += ["__turboadb_ch__", characteristics, "__turboadb_wm__", wm]
    lines += [mark("displays"), displays, mark("end")]
    return ("\r\n".join(lines) + "\r\n").encode()


class _Device:
    """A fake ADBHandler that records every adb child and one-shot command."""

    serial = "FAKE123"
    config = None

    def __init__(self, probe_output=b"default", command_delay=0.0):
        # probe_output None: the probe's adb shell never answers
        self.probe_output = _probe_output() if probe_output == b"default" else probe_output
        self.command_delay = command_delay
        self.children = []  # persistent or streamed children (_Session / _Proc)
        self.commands = []  # one-shot adb commands, by name
        self._lock = threading.Lock()
        self._running = {}
        self.peak = {}  # the most concurrent one-shot commands, per name

    # ---- bookkeeping ----
    def _one_shot(self, name, result=None):
        with self._lock:
            self.commands.append(name)
            self._running[name] = self._running.get(name, 0) + 1
            self.peak[name] = max(self.peak.get(name, 0), self._running[name])
        try:
            if self.command_delay:
                time.sleep(self.command_delay)
            return result
        finally:
            with self._lock:
                self._running[name] -= 1

    def sessions(self):
        """Interactive ``adb shell`` sessions (the Terminal's)."""
        return [c for c in self.children if isinstance(c, _Session)]

    def probes(self):
        """``adb shell <script>`` children (the connect-time probe)."""
        return [c for c in self.children if isinstance(c, _Proc) and c.argv[:1] == ["shell"]]

    def spawned(self, argv0):
        return [c for c in self.children if isinstance(c, _Proc) and c.argv[:1] == [argv0]]

    def alive(self):
        return [c for c in self.children if c.alive]

    # ---- engine API used by the tab ----
    def open_shell(self, tty=True, safe=None):
        session = _Session()
        self.children.append(session)
        return OperationResult(True, "open_shell", value=session)

    def popen(self, args):
        args = list(args)
        streaming = args[:1] == ["logcat"] and "-d" not in args
        proc = _Proc(args, None if streaming else self.probe_output)
        self.children.append(proc)
        return proc

    def shell(self, command, timeout=None, safe=None, **_kw):
        text = "2000|shell|V2318\n" if "id -u" in command else ""
        result = CommandResult(command, 0, text, "", 0.0)
        return self._one_shot(
            "shell", OperationResult(True, "shell", value=result) if safe else result
        )

    def logcat_clear(self, safe=None):
        return self._one_shot("logcat_clear", True)

    def list_displays(self, method="auto", safe=None):
        return self._one_shot("list_displays", OperationResult(True, "list_displays", value=[]))

    def device_info(self, safe=None):
        return self._one_shot("device_info", {"model": "V2318"})

    def list_packages(self, third_party=False, safe=None):
        return self._one_shot("list_packages", OperationResult(True, "pm", value=["com.a"]))

    def get_state(self):
        return self._one_shot("get_state", "device")

    def phone_support(self, safe=None):
        return self._one_shot("phone_support", OperationResult(True, "ps", value={"telephony": True}))

    def call_log(self, limit=100, safe=None):
        return self._one_shot("call_log", OperationResult(True, "calls", value=[]))

    def sms_list(self, limit=100, safe=None):
        return self._one_shot("sms_list", OperationResult(True, "sms", value=[]))

    def call_state(self, safe=None):
        return self._one_shot("call_state", OperationResult(True, "state", value="idle"))

    def install(self, path, grant_perms=False, safe=None):
        return self._one_shot("install", OperationResult(True, "install", value="Success"))

    def install_multiple(self, paths, grant_perms=False, safe=None):
        return self._one_shot("install_multiple", OperationResult(True, "install", value="Success"))

    def reboot(self, mode=None, safe=None):
        return self._one_shot("reboot", OperationResult(True, "reboot", value=True))

    def disconnect(self, safe=None):
        return True


def _tab(qapp, **kwargs):
    from turboadb.gui.device_tab import DeviceTab

    tab = DeviceTab({"name": "phone", "serial": "FAKE123"}, **kwargs)
    # Never auto-connect to the made-up serial: that runs a real
    # `adb -s FAKE123 wait-for-device` which outlives the test.  (conftest
    # also stubs the connect worker; this keeps the module safe on its own.)
    tab._started_connect = True
    tab.resize(1280, 820)
    return tab


def _close(qapp, tab):
    tab.close_session()
    tab.close()
    tab.deleteLater()
    qapp.processEvents()


def _connect(tab, device, payload=None):
    """What the connect worker delivers once its probe has the identity."""
    tab._on_connected(device, dict(payload or {}, _probe_pending=True))


# --------------------------------------------------------------------------- #
# (a) persistent processes only for what is on screen or started
# --------------------------------------------------------------------------- #
def test_hidden_tab_opens_no_adb_shell_until_its_terminal_is_shown(qapp):
    device = _Device()
    tab = _tab(qapp)
    try:
        _connect(tab, device)
        qapp.processEvents()
        assert device.children == []  # connected, but never on screen

        tab.show_subtab("logcat")  # another page is current when the tab appears
        tab.show()
        assert _pump(qapp, lambda: tab._lazy_pages["logcat"].page is not None)
        _pump(qapp, lambda: False, timeout=0.1)
        assert device.sessions() == []

        tab.show_subtab("shell")
        assert _pump(qapp, lambda: len(device.sessions()) == 1)
        tab.show_subtab("apps")
        tab.show_subtab("shell")
        _pump(qapp, lambda: False, timeout=0.2)
        assert len(device.sessions()) == 1  # one shell per tab, reused
        assert device.commands.count("shell") == 0  # the probe answers the prompt
    finally:
        _close(qapp, tab)


def test_logcat_starts_no_process_until_start_is_pressed(qapp):
    device = _Device()
    tab = _tab(qapp)
    try:
        _connect(tab, device)
        tab.show()
        tab.show_subtab("logcat")
        _pump(qapp, lambda: False, timeout=0.3)
        assert device.spawned("logcat") == []

        tab.logcat.start()
        assert _pump(qapp, lambda: len(device.spawned("logcat")) == 1)
        tab.logcat.stop()
        assert _pump(qapp, lambda: not device.spawned("logcat")[0].alive)
    finally:
        _close(qapp, tab)


# --------------------------------------------------------------------------- #
# (b) polls: only while the page is visible, never overlapping themselves
# --------------------------------------------------------------------------- #
def test_call_state_poll_stops_while_the_phone_page_is_hidden(qapp, monkeypatch):
    from turboadb.gui import phone_panel

    monkeypatch.setattr(phone_panel.PhonePanel, "POLL_MS", 30)
    device = _Device(command_delay=0.05)
    tab = _tab(qapp)
    try:
        _connect(tab, device)
        tab.show()
        _pump(qapp, lambda: False, timeout=0.3)
        assert "call_state" not in device.commands  # Phone was never on screen

        tab.show_subtab("phone")
        assert _pump(qapp, lambda: device.commands.count("call_state") >= 3, timeout=5)
        assert device.peak["call_state"] == 1

        tab.show_subtab("logcat")
        _pump(qapp, lambda: False, timeout=0.2)  # a call in flight may still finish
        hidden = device.commands.count("call_state")
        _pump(qapp, lambda: False, timeout=0.5)
        assert device.commands.count("call_state") == hidden
    finally:
        _close(qapp, tab)


def test_call_state_poll_skips_ticks_while_the_last_one_runs(qapp, monkeypatch):
    from turboadb.gui import phone_panel

    monkeypatch.setattr(phone_panel.PhonePanel, "POLL_MS", 20)
    release = threading.Event()
    starts = []

    class Device(_Device):
        def call_state(self, safe=None):
            starts.append(time.monotonic())
            if len(starts) > 1:
                release.wait(5)  # every poll after the first hangs
            return super().call_state(safe=safe)

    device = Device()
    tab = _tab(qapp)
    try:
        _connect(tab, device)
        tab.show()
        tab.show_subtab("phone")
        assert _pump(qapp, lambda: len(starts) == 2, timeout=5)  # the poll is running
        _pump(qapp, lambda: False, timeout=0.3)  # ~15 ticks while that call hangs
        assert len(starts) == 2 and device.peak["call_state"] == 1
        release.set()
        assert _pump(qapp, lambda: len(starts) >= 3, timeout=5)  # polling resumes
    finally:
        release.set()
        _close(qapp, tab)


def test_apps_refresh_burst_runs_one_listing_at_a_time(qapp):
    from turboadb.gui.apps_panel import AppsPanel

    device = _Device(command_delay=0.15)
    panel = AppsPanel(device)
    try:
        for _ in range(6):
            panel.refresh()
        assert _pump(qapp, lambda: panel._all == ["com.a"] and not panel._listing, timeout=5)
        assert device.peak["list_packages"] == 1
        assert device.commands.count("list_packages") == 2  # the first + one follow-up
    finally:
        panel.close_panel()


# --------------------------------------------------------------------------- #
# (c) the connect-time probe: one adb process for identity, prompt, kind, displays
# --------------------------------------------------------------------------- #
# Before: quick identity, prompt, getprop, device kind and a display scan (5).
CONNECT_PROBE_PROCESS_TARGET = 1


def test_connect_probe_is_one_adb_process_and_feeds_every_consumer(qapp):
    from turboadb.config import ADBConfig
    from turboadb.gui.device_tab import _ConnectThread

    device = _Device(probe_output=_probe_output(
        characteristics="automotive",
        displays='DisplayInfo{"Centre, displayId 0", real 1920 x 720, x}\n'
                 'DisplayInfo{"Cluster, displayId 2", real 1920 x 720, x}',
    ))
    tab = _tab(qapp)
    worker = _ConnectThread(ADBConfig(serial="FAKE123"), gate=tab._adb_gate)
    worker.ok.connect(tab._on_connected)
    worker.details.connect(tab._on_probe_details)
    try:
        tab.show()
        worker._probe_and_emit(device)  # what run() does after connect()
        qapp.processEvents()

        assert len(device.probes()) <= CONNECT_PROBE_PROCESS_TARGET
        assert device.commands == []  # no prompt, getprop, kind or display commands
        assert not device.probes()[0].alive

        assert tab.title_label.text() == "vivo V2318"
        assert tab._automotive is True  # classified from the probe's characteristics
        assert [d["id"] for d in tab.mirror_tab._displays] == [0, 2]
        assert tab._subtabs.get("ivi") is not None  # a car with two displays
        android = tab.shell.android_widget
        assert android._prompt_state == "known" and android._prompt_id == "shell@V2318"
        assert android._info.get("kind") == "automotive"

        assert _pump(qapp, lambda: len(device.sessions()) == 1)  # the Terminal opened
        _pump(qapp, lambda: False, timeout=0.2)
        assert device.commands == []  # ...without a separate prompt query
    finally:
        _close(qapp, tab)


def test_device_probe_matches_the_engine_parsers(qapp):
    from turboadb.gui.device_tab import _DeviceProbe

    device = _Device()
    probe = _DeviceProbe(device)
    identities = []
    result = probe.run(on_identity=identities.append)
    assert identities == [{
        "manufacturer": "vivo", "model": "V2318", "android_version": "16", "sdk": "36",
        "device": "V2318", "abi": "arm64-v8a", "_quick_identity": True,
        "_prompt": (False, "shell@V2318"),
    }]
    info = result["info"]
    assert info["serial"] == "FAKE123" and info["build_id"] == "build-1"
    assert info["kind"] == "phone" and info["telephony"] is True
    assert info["display_size"] == "1080x2400" and info["automotive"] is False
    assert result["displays"] == ADBHandler.parse_display_info(
        'DisplayInfo{"Built-in Screen, displayId 0", real 1080 x 2400, x}'
    )
    assert result["prompt"] == (False, "shell@V2318") and result["error"] == ""
    assert len(device.probes()) == 1 and device.commands == []
    script = _DeviceProbe.script()
    for section in _DeviceProbe.SECTIONS:
        assert _DeviceProbe.MARK.format(section) in script
    assert "cmd display get-displays" in script and "dumpsys display" in script


def test_a_failed_probe_still_connects_and_falls_back(qapp):
    from turboadb.gui.device_tab import _DeviceProbe

    failed = _DeviceProbe(_Device(probe_output=b"error: device offline\n"))
    identities = []
    result = failed.run(on_identity=identities.append)
    assert identities == [{}] and result["info"] is None and result["error"]

    device = _Device()
    tab = _tab(qapp)
    messages = []
    tab.log.connect(messages.append)
    try:
        _connect(tab, device)
        tab.show()
        assert _pump(qapp, lambda: len(device.sessions()) == 1)
        _pump(qapp, lambda: False, timeout=0.1)
        assert device.commands == []  # the probe's answer is still on its way
        tab._on_probe_details(device, result)
        assert _pump(qapp, lambda: {"list_displays", "shell"} <= set(device.commands))
        assert any("Device details are still unavailable" in m for m in messages)
    finally:
        _close(qapp, tab)


# --------------------------------------------------------------------------- #
# (d) closing the tab ends every process it started
# --------------------------------------------------------------------------- #
def test_closing_the_tab_ends_every_adb_process_it_started(qapp):
    from turboadb.gui.qtutil import thread_running

    device = _Device(probe_output=None)  # the probe never answers: close must kill it
    tab = _tab(qapp)
    tab.show()
    tab._on_connected(device, None)  # connected directly: the tab runs its own probe
    assert _pump(qapp, lambda: len(device.probes()) == 1 and len(device.sessions()) == 1)
    tab.show_subtab("logcat")
    assert _pump(qapp, lambda: tab._lazy_pages["logcat"].page is not None)
    tab.logcat.start()
    assert _pump(qapp, lambda: len(device.spawned("logcat")) == 1)
    assert len(device.alive()) == 3
    probe_thread = tab._probe_thread

    tab.close_session()
    assert _pump(qapp, lambda: device.alive() == [], timeout=5)
    assert _pump(qapp, lambda: not thread_running(probe_thread), timeout=5)
    tab.close()
    tab.deleteLater()
    qapp.processEvents()


def test_closing_before_pages_were_shown_builds_nothing(qapp):
    device = _Device()
    tab = _tab(qapp)
    _connect(tab, device)
    tab.close_session()
    assert all(holder.page is None for holder in tab._lazy_pages.values())
    assert tab._lazy_pages["files"].ensure_built() is None  # released: never built later
    assert device.children == [] and device.commands == []
    tab.close()


# --------------------------------------------------------------------------- #
# (e) at most ADB_SLOTS one-shot commands of one tab at once
# --------------------------------------------------------------------------- #
def test_background_commands_are_capped_per_tab(qapp):
    from turboadb.gui.device_tab import DeviceTab

    tab = _tab(qapp)
    tab.handler = _Device()
    release = threading.Event()
    lock = threading.Lock()
    state = {"running": 0, "peak": 0, "ran": 0}

    def command():
        with lock:
            state["running"] += 1
            state["ran"] += 1
            state["peak"] = max(state["peak"], state["running"])
        release.wait(5)
        with lock:
            state["running"] -= 1
        return "ok"

    try:
        for index in range(6):
            tab._run_action(f"burst {index}", command, lambda _value: None)
        workers = list(tab._threads)
        _pump(qapp, lambda: False, timeout=0.3)
        assert state["peak"] == DeviceTab.ADB_SLOTS == 2
        tab.handler = None
        tab.close_session()  # the four waiting commands must never start
        release.set()
        for worker in workers:
            assert worker.wait(5000)
        assert state["ran"] == 2
    finally:
        release.set()
        tab.close()


def test_file_browser_and_apps_take_device_slots_but_local_listing_does_not(qapp):
    from turboadb.gui.apps_panel import AppsPanel
    from turboadb.gui.device_tab import _AdbGate
    from turboadb.gui.file_browser import FileBrowser

    wrapped = []

    class Gate(_AdbGate):
        def wrap(self, fn):
            wrapped.append(fn)
            return super().wrap(fn)

    gate = Gate(2)
    device = _Device()
    browser = FileBrowser(device, adb_gate=gate)
    apps = AppsPanel(device, adb_gate=gate)
    try:
        assert wrapped == []  # the drive list and the PC folder listing take no slot
        browser.refresh_remote()
        apps.refresh()
        assert len(wrapped) == 2
    finally:
        browser.close_panel()
        apps.close_panel()
        gate.close()


# --------------------------------------------------------------------------- #
# lazily built pages keep every attribute other code reads
# --------------------------------------------------------------------------- #
def test_lazy_pages_are_built_on_first_show_and_attributes_resolve(qapp, monkeypatch):
    from PyQt5.QtCore import Qt
    from turboadb.gui.apps_panel import AppsPanel
    from turboadb.gui.device_tab import _LazyPage
    from turboadb.gui.file_browser import FileBrowser
    from turboadb.gui.logcat_view import LogcatPanel

    device = _Device()
    tab = _tab(qapp)
    assert not hasattr(tab, "logcat")  # nothing before the tab connects
    try:
        _connect(tab, device)
        pages = tab._lazy_pages
        assert set(pages) == {"logcat", "files", "apps", "phone", "webcam"}
        assert all(holder.page is None for holder in pages.values())
        labels = [tab.inner.tabText(i) for i in range(tab.inner.count())]
        assert labels == ["Terminal", "Logcat", "Files", "Device Control", "Apps", "Phone", "Webcam"]

        tab.show()
        tab.show_subtab("files")
        assert _pump(qapp, lambda: pages["files"].page is not None)
        assert isinstance(tab.files, FileBrowser) and tab.files is pages["files"].page
        assert tab.files.isVisible()
        assert pages["logcat"].page is None and pages["apps"].page is None

        assert isinstance(tab.logcat, LogcatPanel)  # reading the attribute builds it
        saved = []
        monkeypatch.setattr(LogcatPanel, "_save", lambda self: saved.append(self))
        tab.show_subtab("logcat")
        tab.save_active_output()
        assert saved == [tab.logcat]

        # device details that arrived earlier reach an Apps page built later
        tab._automotive = True
        assert isinstance(tab.apps, AppsPanel) and not tab.apps.third.isChecked()

        # split view moves the holders, and showing them builds what they need
        tab._activate_split(["shell", "logcat"], Qt.Horizontal)
        assert tab._split_panes["logcat"].layout().itemAt(1).widget() is pages["logcat"]
        tab._leave_split()
        assert tab.inner.indexOf(pages["logcat"]) == 1
        assert isinstance(tab.inner.widget(1), _LazyPage)
    finally:
        _close(qapp, tab)


def test_android_shell_stop_reuses_the_known_prompt(qapp):
    from turboadb.gui.device_tab import _AndroidShellWidget

    device = _Device()
    widget = _AndroidShellWidget(device, device_name="mock")
    try:
        assert _pump(qapp, lambda: widget._prompt_state == "known")
        assert device.commands.count("shell") == 1  # user@host, once
        widget.interrupt()  # Stop: a fresh shell on the same adbd, same user@host
        _pump(qapp, lambda: False, timeout=0.2)
        assert len(device.sessions()) == 2
        assert device.commands.count("shell") == 1
        widget.reconnect(focus=False)  # adbd may have restarted: ask again
        assert _pump(qapp, lambda: device.commands.count("shell") == 2)
    finally:
        widget.close_panel()
        widget.close()


# --------------------------------------------------------------------------- #
# review regressions
# --------------------------------------------------------------------------- #
def _banner_after_show(qapp, tab):
    """Seconds from showing the tab until the Android terminal's banner is out."""
    started = time.monotonic()
    tab.show()
    android = tab.shell.android_widget
    assert _pump(qapp, lambda: android._banner_shown, timeout=4.0)
    return time.monotonic() - started


def test_terminal_only_tab_shows_its_banner_at_once(qapp):
    """The lazily opened shell must not wait 2.5 s for details that never come."""
    device = _Device()
    tab = _tab(qapp, terminal_only=True)
    try:
        tab._on_connected(device, None)
        assert device.sessions() == []
        assert _banner_after_show(qapp, tab) < 1.0
        assert len(device.sessions()) == 1
    finally:
        _close(qapp, tab)


def test_hidden_tab_with_a_failed_probe_shows_its_banner_at_once(qapp):
    device = _Device()
    tab = _tab(qapp)
    try:
        _connect(tab, device, {"manufacturer": "vivo", "model": "V2318", "_quick_identity": True})
        failed = {"info": None, "prompt": None, "displays": None, "error": "timed out"}
        tab._on_probe_details(device, failed)  # while the tab is still hidden
        assert _banner_after_show(qapp, tab) < 1.0
        assert "vivo V2318" in tab.shell.android_widget._welcome_banner()
    finally:
        _close(qapp, tab)


def test_installs_and_reboot_never_wait_for_an_adb_slot(qapp, monkeypatch):
    """Long installs take no slot, and Reboot runs even when every slot is busy."""
    from turboadb.gui import apps_panel
    from turboadb.gui.apps_panel import AppsPanel

    class Device(_Device):
        def start_app(self, package, safe=None):
            return self._one_shot("start_app", OperationResult(True, "start", value="ok"))

    device = Device()
    tab = _tab(qapp)
    tab.handler = device
    gate = tab._adb_gate
    held = tab.ADB_SLOTS
    for _ in range(held):
        gate.acquire()  # two slow commands are running
    panel = AppsPanel(device, adb_gate=gate)
    sent = []
    try:
        for files in (["one.apk"], ["base.apk", "split.apk"]):
            monkeypatch.setattr(
                apps_panel.QFileDialog, "getOpenFileNames",
                staticmethod(lambda *_a, f=files, **_k: (f, "")),
            )
            panel._install()
        assert _pump(qapp, lambda: {"install", "install_multiple"} <= set(device.commands))

        tab._on_reboot_sent = sent.append  # the recovery flow is not under test
        tab._start_reboot(None)
        assert _pump(qapp, lambda: sent == [None])
        assert device.commands.count("reboot") == 1

        # a short app action still waits for a slot
        panel._on_packages(panel._list_generation, ["com.a"])
        panel.list.setCurrentRow(0)
        panel._start()
        _pump(qapp, lambda: False, timeout=0.2)
        assert "start_app" not in device.commands
        gate.release()
        held -= 1
        assert _pump(qapp, lambda: "start_app" in device.commands)
    finally:
        panel.close_panel()
        tab.handler = None
        _close(qapp, tab)
        for _ in range(held):
            gate.release()


def test_closing_a_split_view_tab_starts_no_adb_shell(qapp):
    from PyQt5.QtCore import Qt

    device = _Device()
    tab = _tab(qapp)
    try:
        _connect(tab, device)
        tab.show_subtab("logcat")
        tab.show()
        _pump(qapp, lambda: tab._lazy_pages["logcat"].page is not None)
        tab._activate_split(["logcat", "files"], Qt.Horizontal)
        assert _pump(qapp, lambda: "shell" in device.commands)  # the Files pane's listing
        _pump(qapp, lambda: False, timeout=0.2)
        assert device.sessions() == []  # the Terminal was never on screen
        commands = list(device.commands)

        tab.close_session()  # leaves split view, which re-shows the tab strip
        _pump(qapp, lambda: False, timeout=0.3)
        assert device.sessions() == []
        assert device.children == [] and device.commands == commands
    finally:
        _close(qapp, tab)


def test_direct_connect_on_a_visible_tab_asks_no_separate_prompt(qapp):
    device = _Device()
    tab = _tab(qapp)
    try:
        tab.show()
        tab._on_connected(device, None)  # the Terminal is on screen at once
        assert _pump(qapp, lambda: tab.shell.android_widget._prompt_state == "known")
        _pump(qapp, lambda: False, timeout=0.2)
        assert len(device.sessions()) == 1 and len(device.probes()) == 1
        assert device.commands.count("shell") == 0  # the probe answered user@host
    finally:
        _close(qapp, tab)


def test_connected_is_announced_once_after_the_tab_is_named(qapp):
    device = _Device()
    tab = _tab(qapp)
    named = []
    tab.connected.connect(lambda: named.append(tab.title_label.text()))
    try:
        _connect(tab, device, {"_quick_identity": True, "manufacturer": "vivo", "model": "V2318"})
        assert named == []  # never from inside the connect slot
        qapp.processEvents()
        assert named == ["vivo V2318"]
        tab._announce_connected()  # a later reconnect is not a new connection
        qapp.processEvents()
        assert named == ["vivo V2318"]
    finally:
        _close(qapp, tab)


def test_a_tab_closed_before_its_announcement_reports_no_connection(qapp):
    device = _Device()
    tab = _tab(qapp)
    fired = []
    tab.connected.connect(lambda: fired.append(True))
    _connect(tab, device)
    _close(qapp, tab)  # closes before the queued announcement runs
    assert fired == []


def test_esc_in_a_split_terminal_is_the_terminals_while_device_control_is_maximized(qapp):
    """Issue: with Terminal | Device Control and Maximize on, Esc typed in the
    terminal restored Device Control and never cleared the terminal's line."""
    from PyQt5.QtCore import Qt
    from PyQt5.QtTest import QTest

    device = _Device()
    tab = _tab(qapp)
    tab.setAttribute(Qt.WA_DontShowOnScreen, True)
    try:
        _connect(tab, device, {"_quick_identity": True, "manufacturer": "vivo", "model": "V2318"})
        tab.show()
        qapp.setActiveWindow(tab)
        tab._activate_split(["shell", "controls"], Qt.Horizontal)
        term = tab.shell.android_widget.term
        mirror = tab.mirror_tab
        assert _pump(qapp, lambda: term._alive and term.isVisible() and mirror.isVisible())

        mirror.btn_max.click()  # focus was nowhere in Device Control: it moves there
        assert mirror.act_max.isChecked() and tab._control_view.controls_card.isHidden()
        term.setFocus()
        assert _pump(qapp, lambda: qapp.focusWidget() is term)
        QTest.keyClicks(term, "cat /proc/cpuinfo")
        assert term._line == "cat /proc/cpuinfo"
        QTest.keyClick(term, Qt.Key_Escape)
        qapp.processEvents()
        assert term._line == ""  # the terminal cleared its line
        assert mirror.act_max.isChecked()  # and Device Control stayed maximized

        # with the focus in Device Control, Esc restores it
        mirror.btn_opts.setFocus()
        assert _pump(qapp, lambda: qapp.focusWidget() is mirror.btn_opts)
        QTest.keyClick(mirror.btn_opts, Qt.Key_Escape)
        qapp.processEvents()
        assert not mirror.act_max.isChecked()
        assert not tab._control_view.controls_card.isHidden()

        # Maximize chosen while the terminal has the keyboard: Esc right after
        # the click restores (the focus moved to Device Control's toolbar)
        term.setFocus()
        assert _pump(qapp, lambda: qapp.focusWidget() is term)
        mirror.btn_max.click()
        assert mirror.isAncestorOf(qapp.focusWidget())
        QTest.keyClick(qapp.focusWidget(), Qt.Key_Escape)
        qapp.processEvents()
        assert not mirror.act_max.isChecked()
    finally:
        tab.hide()
        _close(qapp, tab)


def test_engine_diagnostics_reach_the_tabs_trace_not_its_notifications(qapp):
    """Every engine line ("[DEBUG] $ adb …", "[ERROR] uninstall failed: …")
    went to DeviceTab.log, which raises toasts, duplicating what the panel
    that ran the command reports itself."""
    tab = _tab(qapp)
    logged, traced = [], []
    tab.log.connect(logged.append)
    tab.trace.connect(traced.append)
    def refused():
        raise RuntimeError("DELETE_FAILED")

    try:
        handler = tab._ct._make_handler()  # as the connect worker builds it
        result = handler._guard("uninstall", refused)
        assert not result.success
        assert traced == ["[ERROR] uninstall failed: DELETE_FAILED"]
        assert logged == []
    finally:
        _close(qapp, tab)
