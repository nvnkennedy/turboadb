"""The Android shell when the device, adbd or the adb server goes away.

* A shell that could not be opened (after Stop, Restart shell or a
  reconnect) left a normal-looking prompt that swallowed every command:
  the terminal now says so, shows no prompt, and the device tab
  brings the shell back.
* While adbd restarts on purpose (adb root, making files writable) the
  shell-lost and reconnect paths reconnected the shell and re-enabled the
  actions in the middle of it; a Terminal first opened then started a
  shell that could only die.
* A failed Restart ADB left the shell paused for good while it said it
  would reconnect by itself.
* Reboots, ADB restarts and adbd holds bring a device-terminal shell back
  as a device-terminal shell.
"""

import pytest

pytest.importorskip("PyQt5")

from turboadb.results import OperationResult  # noqa: E402

from test_android_shell_pty import (  # noqa: E402
    _Handler,
    _Mksh,
    _close,
    _enter,
    _pump,
    _settle,
)


def _android(qapp, handler):
    from turboadb.gui.device_tab import _AndroidShellWidget

    widget = _AndroidShellWidget(handler, device_name="mock", info={"kind": "phone"},
                                 autostart=False)
    widget.resize(900, 500)
    lost = []
    widget.disconnected.connect(lambda: lost.append(True))
    return widget, lost


# --------------------------------------------------------------------------- #
# a shell that could not be opened
# --------------------------------------------------------------------------- #
def test_a_failed_open_leaves_a_dead_terminal_not_a_live_prompt(qapp):
    handler = _Handler()
    handler.fail = RuntimeError("adb not found")
    widget, lost = _android(qapp, handler)
    try:
        term = widget.term
        widget.ensure_started()
        assert not term._alive and lost == [True]  # the device tab takes over
        assert _pump(qapp, lambda: "[could not open adb shell: adb not found]" in term.toPlainText())
        assert "$ " not in term.toPlainText()  # no prompt that would swallow commands
        _enter(term, "ls")  # typing goes nowhere, visibly
        assert "ls" not in term.toPlainText()

        widget.interrupt()  # Stop tries again
        assert handler.opened == [True, True] and not term._alive
        widget.reconnect(focus=False)  # and so does a reconnect
        assert handler.opened == [True, True, True] and not term._alive
        assert lost == [True, True, True]

        handler.fail = None
        widget.restart_shell()
        assert term._alive and len(handler.sessions) == 1
        _settle(qapp, widget, "PD2318:/ $ ")
    finally:
        _close(widget)


# --------------------------------------------------------------------------- #
# device tab flows
# --------------------------------------------------------------------------- #
@pytest.fixture
def device_tab(qapp):
    from test_adb_processes import _Device, _close as close_tab, _connect, _tab

    class Device(_Device):
        """Hands the Terminal device-terminal shells (:class:`_Mksh`)."""

        def __init__(self):
            super().__init__()
            self.shells = []

        def open_shell(self, tty=True, safe=None):
            shell = _Mksh(tty)
            self.shells.append(shell)
            return OperationResult(True, "open_shell", value=shell)

    device = Device()
    tab = _tab(qapp)
    try:
        _connect(tab, device)
        yield tab, device
    finally:
        close_tab(qapp, tab)


def _terminal(qapp, tab, device):
    """Show the Terminal and wait for its device prompt."""
    tab.show()
    tab.show_subtab("shell")
    widget = tab.shell.android_widget
    assert _pump(qapp, lambda: len(device.shells) == 1)
    widget.update_identity({"kind": "phone"})  # the banner (and the prompt) need not wait
    _settle(qapp, widget, "PD2318:/ $ ")
    return widget


def test_the_adbd_hold_keeps_the_lost_shell_paths_away(qapp, device_tab, monkeypatch):
    """adb root / making files writable restart adbd on purpose; the
    shell that dies with it is brought back by _release_terminals only."""
    tab, device = device_tab
    widget = _terminal(qapp, tab, device)
    resumed = []
    monkeypatch.setattr(tab, "_logcat_recovery", lambda action, **_kw: resumed.append(action))
    tab._hold_terminals("adb root — adbd restarting")
    assert not device.shells[0].running and not widget.term._alive
    assert not tab.btn_mirror.isEnabled()

    states = device.commands.count("get_state")
    tab._on_shell_lost()  # the shell died with adbd...
    tab._on_shell_lost_state(tab.handler, "device")  # ...or its answer came late
    tab._on_reconnected(True)  # a reconnect that raced the hold
    _pump(qapp, lambda: False, timeout=0.2)
    assert device.commands.count("get_state") == states and not tab._reconnecting
    assert len(device.shells) == 1 and not tab.btn_mirror.isEnabled()
    assert resumed == []

    tab._release_terminals()
    assert tab.btn_mirror.isEnabled()
    assert _pump(qapp, lambda: len(device.shells) == 2)
    assert device.shells[1].tty  # a device terminal again
    _settle(qapp, widget, "PD2318:/ $ ")
    assert resumed == ["resume_after_reconnect"]  # a Logcat capture that ended resumes


def test_a_terminal_first_shown_during_the_hold_opens_after_it(qapp, device_tab):
    """Opened during the hold, its shell would only die with adbd."""
    tab, device = device_tab
    tab.show_subtab("logcat")
    tab.show()
    assert _pump(qapp, lambda: tab._lazy_pages["logcat"].page is not None)
    tab._hold_terminals("Making device files writable — the device may reboot")
    tab.show_subtab("shell")
    widget = tab.shell.android_widget
    widget.update_identity({"kind": "phone"})
    assert _pump(qapp, lambda: "reconnect automatically" in widget.term.toPlainText())
    assert device.shells == [] and not widget.term._alive
    tab._release_terminals()
    assert _pump(qapp, lambda: len(device.shells) == 1)
    _settle(qapp, widget, "PD2318:/ $ ")
    assert widget.term._alive


def test_a_failed_adb_restart_offers_reconnect_and_frees_the_shell(qapp, device_tab):
    """Nothing reconnects after a failed Restart ADB, so the tab offers
    Reconnect and the shell no longer claims it will come back by itself."""
    tab, device = device_tab
    widget = _terminal(qapp, tab, device)
    tab.prepare_for_adb_restart()
    assert widget._adb_restart_paused and not widget.term._alive
    widget.interrupt()  # Stop while paused: nothing to stop, nothing opened
    assert len(device.shells) == 1
    tab.finish_adb_restart(False)
    assert not tab.btn_reconnect.isHidden()
    assert not widget._adb_restart_paused
    assert _pump(qapp, lambda: "ADB restart failed — press Reconnect or Restart shell"
                 in widget.term.toPlainText())
    widget.restart_shell()
    assert len(device.shells) == 2 and widget.term._alive
    _settle(qapp, widget, "PD2318:/ $ ")


def test_a_successful_adb_restart_brings_the_terminal_shell_back(qapp, device_tab):
    tab, device = device_tab
    widget = _terminal(qapp, tab, device)
    tab.prepare_for_adb_restart()
    tab.finish_adb_restart(True)  # waits for the device, then reconnects
    assert _pump(qapp, lambda: len(device.shells) == 2, timeout=6.0)
    assert device.shells[1].tty
    _settle(qapp, widget, "PD2318:/ $ ")
    text = widget.term.toPlainText()
    assert text.endswith("ADB server restarting — this shell will reconnect automatically.\n"
                         "PD2318:/ $ ")


def test_a_reboot_pauses_the_terminal_shell_and_brings_it_back(qapp, device_tab):
    tab, device = device_tab
    widget = _terminal(qapp, tab, device)
    _enter(widget.term, "cd /sdcard")
    _settle(qapp, widget, "PD2318:/sdcard $ ")
    tab._reboot_in_progress = True
    tab.shell.pause_for_device_reboot()
    assert not device.shells[0].running
    assert _pump(qapp, lambda: "Device rebooting — this shell will reconnect automatically."
                 in widget.term.toPlainText())
    tab._on_reconnected(True)  # the device is back
    assert len(device.shells) == 2 and device.shells[1].tty
    # a rebooted device's shell starts at / (and so does the prompt)
    assert b"cd " not in device.shells[1].sent[0]
    _settle(qapp, widget, "reconnect automatically.\nPD2318:/ $ ")
    assert widget.term._cwd == "/"
