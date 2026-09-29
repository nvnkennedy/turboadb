"""The terminal's Send key menu offers only keys that mean something there.

Tab, Esc, the arrows, Backspace and Ctrl+D are keys for a terminal (the
Android shell on a device terminal, or an adb shell typed in PowerShell/CMD).
A shell reading a pipe took them as characters of the next command line, so
"Up" made cmd run ``←[A dir``; they are offered only where a terminal reads
them."""

import pytest

pytest.importorskip("PyQt5")

RAW_KEYS = {"Tab (\\t)": b"\t", "Esc": b"\x1b", "Ctrl+D (EOF)": b"\x04",
            "Up": b"\x1b[A", "Down": b"\x1b[B", "Backspace": b"\x7f"}


def _menu_keys(term):
    """``{label: enabled}`` of the Send key submenu."""
    menu = term._build_menu()
    try:
        keys = next(a.menu() for a in menu.actions() if a.text() == "Send key")
        return {a.text(): a.isEnabled() for a in keys.actions() if not a.isSeparator()}
    finally:
        menu.deleteLater()


class _LocalSession:
    def __init__(self, *_args, **_kwargs):
        self.sent = []
        self.running = True
        self.env = {}

    def send(self, data):
        self.sent.append(data)
        return True

    def read(self, _size=4096):
        return b""

    def interrupt(self):
        self.running = False

    def close(self):
        self.running = False


@pytest.fixture
def local(qapp, monkeypatch):
    from turboadb.gui import local_terminal
    from turboadb.gui.device_tab import _LocalShellWidget

    monkeypatch.setattr(local_terminal, "LocalShellSession", _LocalSession)
    widget = _LocalShellWidget("cmd", serial="V2318")
    widget.ensure_started()
    yield widget
    widget.close_panel()
    widget.deleteLater()


def test_a_local_shell_is_offered_only_enter_ctrl_c_and_ctrl_z(local):
    keys = _menu_keys(local.term)
    assert {"Enter (\\n)", "Ctrl+C", "Ctrl+Z"} <= set(keys)
    assert not set(RAW_KEYS) & set(keys)
    assert any("need a device terminal" in label and not enabled
               for label, enabled in keys.items())
    sent = len(local.session.sent)
    for data in RAW_KEYS.values():
        assert local.term.send_key(data) is False
    assert len(local.session.sent) == sent  # nothing typed into the next command
    assert local.term.send_key(b"\n") is True and local.session.sent[-1] == b"\n"


def test_inside_an_adb_shell_every_key_is_offered(local):
    local._enter_adb_shell("adb shell -t -t")
    keys = _menu_keys(local.term)
    assert set(RAW_KEYS) <= set(keys)
    assert not any("need a device terminal" in label for label in keys)
    assert local.term.send_key(b"\x1b[A") is True and local.session.sent[-1] == b"\x1b[A"
    assert local.term.send_key(b"\x1a") is False  # still refused: adb.exe's end of input


def test_the_android_shell_offers_them_on_a_device_terminal_only(qapp):
    from test_android_shell_pty import _Handler, _Mksh, _close, _ready, _widget

    widget, handler = _widget(qapp)
    try:
        _ready(qapp, widget)
        assert set(RAW_KEYS) <= set(_menu_keys(widget.term))
        assert widget.term.send_key(b"\t") is True and handler.sessions[0].sent[-1] == b"\t"
    finally:
        _close(widget)

    from turboadb.gui import settings as settings_mod

    settings_mod.set("android_shell_pty", False)
    try:
        widget, handler = _widget(qapp, _Handler(make=lambda tty: _Mksh(tty=False)))
        try:
            assert not set(RAW_KEYS) & set(_menu_keys(widget.term))
            sent = len(handler.sessions[0].sent)
            assert widget.term.send_key(b"\t") is False
            assert len(handler.sessions[0].sent) == sent
        finally:
            _close(widget)
    finally:
        settings_mod.set("android_shell_pty", True)
