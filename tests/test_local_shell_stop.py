"""Stop / Ctrl+C in the PowerShell and CMD terminals.

Inside an ``adb shell`` started there, Stop interrupts the device command and
keeps the adb shell; a stuck adb shell is reopened in its device folder. A
local command is still hard-stopped with a fresh local shell.
"""
import pytest

pytest.importorskip("PyQt5")


class FakeSession:
    running = True

    def __init__(self):
        self.sent = []

    def send(self, data):
        self.sent.append(data)

    def close(self):
        self.running = False


@pytest.fixture
def shell(qapp, monkeypatch, tmp_path):
    """A PowerShell widget on fake sessions; ``shell.sessions`` lists every
    session opened, ``shell.stops`` every stop (True = interrupt)."""
    from turboadb.gui.device_tab import _LocalShellWidget

    widget = _LocalShellWidget("powershell", serial="V2318")
    widget._shell_cwd = str(tmp_path)
    sessions, stops = [], []

    def start_session(*, show_banner=True):
        widget.session = FakeSession()
        sessions.append(widget.session)

    def stop_session(*, interrupt=False):
        stops.append(interrupt)
        if widget.session is not None:
            widget.session.running = False
        widget.session = None

    monkeypatch.setattr(widget, "_start_session", start_session)
    monkeypatch.setattr(widget, "_stop_session", stop_session)
    widget._started = True
    start_session()
    widget.sessions, widget.stops = sessions, stops
    try:
        yield widget
    finally:
        widget.close_panel()
        widget.deleteLater()


def _output(widget, text):
    widget._feed_from(widget.reader, text.encode("utf-8"))


def test_stop_in_adb_shell_interrupts_the_device_command_and_stays(shell):
    shell._send(b"adb shell\r\n")
    _output(shell, "PD2318:/ $ ")
    shell._send(b"cd /sdcard\r\n")
    _output(shell, "PD2318:/sdcard $ ")
    shell._send(b"logcat\r\n")
    _output(shell, "09-17 10:00:00.000 I Tag: message\r\n")

    shell.interrupt()
    session = shell.sessions[-1]
    assert session.sent[-1] == b"\x03"
    assert shell.stops == [] and len(shell.sessions) == 1  # no new local shell
    assert shell._in_adb_shell and shell._adb_shell_cwd == "/sdcard"
    assert shell.term._completion_fn == shell._adb_shell_complete
    assert shell._adb_interrupt_timer.isActive()

    _output(shell, "^C\r\n130|PD2318:/sdcard $ ")  # the device prompt is back
    assert not shell._adb_interrupt_pending and not shell._adb_interrupt_timer.isActive()

    shell.interrupt()  # the next Stop is another Ctrl+C, not a reopen
    assert session.sent[-1] == b"\x03" and shell.stops == []


def test_a_stuck_adb_shell_is_reopened_in_its_device_folder(shell):
    shell._send(b"adb -s V2318 shell\r\n")
    _output(shell, "PD2318:/ $ ")
    shell._send(b"cd /sdcard/Download\r\n")
    _output(shell, "PD2318:/sdcard/Download $ ")

    shell.interrupt()
    shell._on_adb_interrupt_timeout()  # not even the ^C echo came back

    assert shell.stops == [True] and len(shell.sessions) == 2
    fresh = shell.sessions[-1]
    assert fresh.sent == [b"adb -s V2318 shell -t -t\r\n"]
    assert shell._in_adb_shell and shell._adb_shell_cwd == "/sdcard/Download"
    assert shell.term._completion_fn == shell._adb_shell_complete

    _output(shell, "PD2318:/ $ ")  # the reopened shell starts at /
    assert fresh.sent[-1] == b"cd /sdcard/Download\r\n"
    _output(shell, "cd /sdcard/Download\r\nPD2318:/sdcard/Download $ ")
    assert len(fresh.sent) == 2  # moved back once only


def test_a_second_stop_before_the_prompt_returns_reopens_the_adb_shell(shell):
    shell._send(b"adb shell\r\n")
    _output(shell, "PD2318:/ $ ")
    shell._send(b"top\r\n")

    shell.interrupt()
    _output(shell, "^C")  # echoed, but the command ignored it
    shell._on_adb_interrupt_timeout()
    assert shell.stops == []  # the adb shell answered, so it is not reopened by itself

    shell.interrupt()
    assert shell.stops == [True]
    assert shell.sessions[-1].sent == [b"adb shell -t -t\r\n"]
    assert shell._in_adb_shell


def test_typing_after_a_ctrl_c_lets_the_next_stop_interrupt_again(shell):
    shell._send(b"adb shell\r\n")
    _output(shell, "PD2318:/ $ ")
    shell.interrupt()
    _output(shell, "^C")
    shell._send(b"q\r\n")
    shell.interrupt()
    assert shell.stops == [] and shell.sessions[-1].sent[-1] == b"\x03"


def test_a_reopen_whose_local_shell_cannot_start_leaves_the_adb_shell_context(
        shell, monkeypatch):
    shell._send(b"adb shell\r\n")
    _output(shell, "PD2318:/ $ ")
    shell.interrupt()
    monkeypatch.setattr(shell, "_start_session", lambda *, show_banner=True: None)
    shell._on_adb_interrupt_timeout()
    assert shell.session is None and not shell._in_adb_shell
    assert shell.term._completion_fn == shell._local_complete


def test_stop_on_a_local_command_still_opens_a_fresh_local_shell(shell):
    shell._send(b"ping -t 127.0.0.1\r\n")
    shell.interrupt()
    assert shell.stops == [True] and len(shell.sessions) == 2
    assert shell.sessions[-1].sent == []  # nothing re-entered
    assert not shell._in_adb_shell and b"\x03" not in shell.sessions[0].sent


def test_an_adb_shell_that_ends_by_itself_hands_stop_back_to_the_local_shell(shell, tmp_path):
    local_prompt = f"PS {tmp_path}> "
    shell._send(b"adb shell\r\n")
    # a local prompt still on its way from before the command is not the end
    _output(shell, "\r\n" + local_prompt)
    assert shell._in_adb_shell

    _output(shell, "PD2318:/ $ ")
    _output(shell, "\r\n" + local_prompt)  # the device was unplugged
    assert not shell._in_adb_shell
    assert shell.term._completion_fn == shell._local_complete
    assert shell._shell_cwd == str(tmp_path)

    shell.interrupt()
    assert shell.stops == [True]  # a local Stop again


def test_an_adb_error_ends_the_adb_shell_context(shell, tmp_path):
    shell._send(b"adb shell\r\n")
    _output(shell, "adb.exe: no devices/emulators found\r\n")
    _output(shell, f"PS {tmp_path}> ")
    assert not shell._in_adb_shell


def test_exit_and_close_clear_a_pending_ctrl_c(shell):
    shell._send(b"adb shell\r\n")
    _output(shell, "PD2318:/ $ ")
    shell.interrupt()
    assert shell._adb_interrupt_timer.isActive()
    shell._send(b"exit\r\n")
    assert not shell._in_adb_shell and not shell._adb_interrupt_timer.isActive()

    shell._send(b"adb shell\r\n")
    shell.interrupt()
    shell.close_panel()
    assert not shell._adb_interrupt_timer.isActive()
    shell._on_adb_interrupt_timeout()  # a late timeout after closing does nothing
    assert shell.stops.count(True) == 0
