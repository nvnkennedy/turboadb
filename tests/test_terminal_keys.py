"""Keys a terminal needs that the window has shortcuts for.

The main window binds Ctrl+T, Ctrl+W, Ctrl+Return, Ctrl+B, Ctrl+N, Ctrl+S,
Ctrl+Q and F1, and its menus Alt+letter.  A shortcut takes its key before the
widget with the focus sees it, so Ctrl+W typed in a terminal closed the whole
device tab (instead of deleting a word), and a full-screen program (nano, vi,
htop) never got those keys.  The console now keeps the keys it handles
itself in line mode, and every Ctrl, Alt and function key while a program
has the screen; the window keeps the others."""

import pytest

pytest.importorskip("PyQt5")

from PyQt5.QtCore import Qt  # noqa: E402

# what the main window binds (its menus' mnemonics as Alt+F), and Ctrl+L,
# which the console handles too and a window could bind as well
WINDOW_KEYS = ("Ctrl+T", "Ctrl+W", "Ctrl+Return", "Ctrl+B", "Ctrl+N", "Ctrl+S", "Ctrl+Q",
               "F1", "Alt+F", "Ctrl+L")
CTRL, ALT = Qt.ControlModifier, Qt.AltModifier
# the window's keys the console does not use in line mode, and their shortcut
OTHERS = [
    (Qt.Key_T, CTRL, "Ctrl+T"), (Qt.Key_B, CTRL, "Ctrl+B"), (Qt.Key_N, CTRL, "Ctrl+N"),
    (Qt.Key_S, CTRL, "Ctrl+S"), (Qt.Key_Q, CTRL, "Ctrl+Q"), (Qt.Key_F1, Qt.NoModifier, "F1"),
    (Qt.Key_Return, CTRL, "Ctrl+Return"), (Qt.Key_F, ALT, "Alt+F"),
]


@pytest.fixture
def window(qapp):
    """A window with the main window's shortcuts and a focused terminal in
    it: ``(terminal, shortcuts that went off, lines sent to the shell)``."""
    from PyQt5.QtGui import QKeySequence
    from PyQt5.QtTest import QTest
    from PyQt5.QtWidgets import QShortcut, QVBoxLayout, QWidget

    from turboadb.gui.console import AnsiConsole

    win = QWidget()
    sent, hits = [], []
    term = AnsiConsole(send_fn=sent.append)
    term.set_emulate_prompt(False)
    QVBoxLayout(win).addWidget(term)
    for seq in WINDOW_KEYS:
        QShortcut(QKeySequence(seq), win, activated=lambda seq=seq: hits.append(seq))
    win.resize(700, 400)
    win.show()
    win.activateWindow()
    assert QTest.qWaitForWindowActive(win, 5000)
    term.setFocus(Qt.OtherFocusReason)
    qapp.processEvents()
    assert qapp.focusWidget() is term
    yield term, hits, sent
    term.close_archive()
    win.close()
    win.deleteLater()


def _press(term, key, mods=Qt.NoModifier):
    from PyQt5.QtTest import QTest

    QTest.keyClick(term, key, mods)  # through the shortcut map, as a real key


def _render(term):
    while term._inq:
        term._drain_tick()


def test_ctrl_w_in_a_terminal_deletes_a_word_and_the_tab_stays(window):
    from PyQt5.QtTest import QTest

    term, hits, _sent = window
    QTest.keyClicks(term, "echo hello world")
    _press(term, Qt.Key_W, CTRL)
    assert term._line == "echo hello " and hits == []
    _press(term, Qt.Key_W, CTRL)
    assert term._line == "echo " and hits == []


def test_the_console_keeps_its_own_keys_and_the_window_the_others(window):
    term, hits, sent = window
    term.feed("old output\r\n")
    _render(term)
    _press(term, Qt.Key_L, CTRL)  # the console's clear
    assert "old output" not in term.toPlainText() and hits == []
    for key, mods, _name in OTHERS:
        _press(term, key, mods)
    assert hits == [name for _key, _mods, name in OTHERS]
    assert sent == []  # Ctrl+Return stayed the window's: no line went to the shell


def test_a_program_on_the_screen_gets_every_key(window):
    term, hits, _sent = window
    keys = []
    term.set_screen_input(lambda data: keys.append(data) is None)
    term.feed("\x1b[?1049h\x1b[H\x1b[2J  GNU nano")
    _render(term)
    assert term.has_screen()
    _press(term, Qt.Key_W, CTRL)  # nano's search
    for key, mods, _name in OTHERS:
        _press(term, key, mods)
    assert hits == []
    assert keys == [
        b"\x17", b"\x14", b"\x02", b"\x0e", b"\x13", b"\x11", b"\x1bOP", b"\n", b"\x1bf",
    ]
    # the program leaves: the window has its keys back
    term.feed("\x1b[?1049l")
    _render(term)
    assert not term.has_screen()
    _press(term, Qt.Key_T, CTRL)
    assert hits == ["Ctrl+T"] and len(keys) == 9
