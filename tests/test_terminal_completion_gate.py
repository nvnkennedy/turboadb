"""Tab completion in the device terminals takes one of the tab's adb slots.

Every Tab press ran an ``adb shell ls -d ...`` one-shot outside the device
tab's _AdbGate, on top of whatever the tab was already running.
"""
import threading

import pytest

pytest.importorskip("PyQt5")

from turboadb.results import CommandResult, OperationResult  # noqa: E402


class _Shell:
    serial = "PHONE1"
    config = None

    def __init__(self):
        self.commands = []

    def shell(self, command, timeout=None, safe=None, **_kw):
        self.commands.append(command)
        value = CommandResult(command, 0, "/system/bin/logcat\n", "", 0.0)
        return OperationResult(True, "shell", value=value)


def test_a_completion_query_waits_for_a_slot(qapp):
    from turboadb.gui.device_tab import _AdbGate, _TerminalWidgetBase

    handler = _Shell()
    widget = _TerminalWidgetBase(handler)
    gate = _AdbGate(1)
    widget.adb_gate = gate
    gate.acquire()  # a Files listing holds the tab's only slot
    answers = []
    worker = threading.Thread(
        target=lambda: answers.append(widget._query_android_completion("logc", "/")))
    try:
        worker.start()
        worker.join(0.3)
        assert handler.commands == []  # still waiting for the slot
        gate.release()
        worker.join(5)
        assert len(handler.commands) == 1 and handler.commands[0].startswith("ls -d ")
        assert answers == [("logcat ", [])]
    finally:
        gate.close()
        worker.join(5)
        widget.deleteLater()


class _Listing(_Shell):
    """A device whose `ls` prints *text*."""

    def __init__(self, text):
        super().__init__()
        self.text = text

    def shell(self, command, timeout=None, safe=None, **_kw):
        self.commands.append(command)
        return OperationResult(True, "shell", value=CommandResult(command, 0, self.text, "", 0.0))


@pytest.mark.parametrize("line, listing, answer", [
    # the report: "/da" offered dalvikvm and date, from the bin folders
    ("/da", "/data/\n/data_mirror/\n", ("/data", ["/data/", "/data_mirror/"])),
    ("/sd", "/sdcard/\n", ("/sdcard/", [])),
    ("./ru", "./run.sh\n", ("./run.sh", [])),
    ("sdcard/Do", "sdcard/Documents/\nsdcard/Download/\n",
     (None, ["sdcard/Documents/", "sdcard/Download/"])),
])
def test_a_first_word_with_a_slash_is_completed_as_a_path(qapp, line, listing, answer):
    from turboadb.gui.device_tab import _TerminalWidgetBase

    handler = _Listing(listing)
    widget = _TerminalWidgetBase(handler)
    try:
        assert widget._query_android_completion(line, "/") == answer
        assert len(handler.commands) == 1
        assert "/system/bin" not in handler.commands[0]
        assert f"ls -dp '{line}'*" in handler.commands[0]
    finally:
        widget.deleteLater()


def test_a_first_word_and_a_later_one_complete_the_same_path(qapp):
    from turboadb.gui.device_tab import _TerminalWidgetBase

    listing = "/data/\n/data_mirror/\n"
    first, later = _Listing(listing), _Listing(listing)
    widget = _TerminalWidgetBase(first)
    try:
        options = widget._query_android_completion("/da", "/")[1]
        widget.handler = later
        assert widget._query_android_completion("test /da", "/")[1] == options
    finally:
        widget.deleteLater()


def test_a_first_word_without_a_slash_is_still_a_command(qapp):
    from turboadb.gui.device_tab import _TerminalWidgetBase

    handler = _Listing("/system/bin/dalvikvm\n/system/bin/date\n")
    widget = _TerminalWidgetBase(handler)
    try:
        assert widget._query_android_completion("da", "/") == (None, ["dalvikvm", "date"])
        assert handler.commands[0].startswith("ls -d /system/bin/")
    finally:
        widget.deleteLater()


def test_a_closed_tab_completes_nothing(qapp):
    from turboadb.gui.device_tab import _AdbGate, _TerminalWidgetBase

    handler = _Shell()
    widget = _TerminalWidgetBase(handler)
    gate = _AdbGate(2)
    gate.close()
    widget.adb_gate = gate
    try:
        assert widget._query_android_completion("ls /sd", "/") == (None, [])
        assert handler.commands == []
    finally:
        widget.deleteLater()


def test_the_device_tab_gives_every_terminal_its_gate(qapp):
    from turboadb.gui.device_tab import DeviceTab

    tab = DeviceTab({"name": "phone", "serial": "PHONE1"})
    tab._started_connect = True
    try:
        tab._on_connected(_Shell(), {"_probe_pending": True})
        shell = tab.shell
        for widget in (shell.android_widget, shell.ps_widget, shell.cmd_widget):
            assert widget.adb_gate is tab._adb_gate
    finally:
        tab.handler = None
        tab.close_session()
        tab.close()
