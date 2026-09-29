"""The log's levels: one reading of a message's [TAG] for the log panel and
for the window's status bar and toasts, and a multi-line message keeps its
level on every line (a traceback under "Errors only", a command trace hidden
by default), on screen and in a saved log."""

import types

import pytest

pytest.importorskip("PyQt5")


@pytest.fixture
def panel(qapp):
    from turboadb.gui.log_panel import LogPanel

    widget = LogPanel()
    yield widget
    widget.deleteLater()


def _shown(panel):
    return panel.view.toPlainText().splitlines()


def _levels(panel):
    return [level for _ts, level, _msg in panel._entries]


def _show(panel, label):
    from turboadb.gui.log_panel import _FILTERS

    panel.level_box.setCurrentIndex([name for name, _rank in _FILTERS].index(label))


_TRACEBACK = (
    "[ERROR] Unexpected error:\n"
    "Traceback (most recent call last):\n"
    '  File "worker.py", line 12, in run\n'
    "RuntimeError: boom"
)


def test_a_traceback_keeps_the_error_level_of_its_first_line(panel):
    panel.append(_TRACEBACK)
    assert _levels(panel) == ["ERROR"] * 4
    _show(panel, "Errors only")
    lines = _shown(panel)
    assert len(lines) == 4 and all(" err  " in line for line in lines)
    assert lines[-1].endswith("RuntimeError: boom")


def test_a_saved_log_under_errors_only_has_the_whole_error(panel, tmp_path, monkeypatch):
    from turboadb.gui import fileutil

    writes = []
    monkeypatch.setattr(fileutil, "save_output",
                        lambda _parent, _title, _name, write, **_kw: writes.append(write))
    panel.append("[INFO] Opening 'Pixel'…")
    panel.append(_TRACEBACK)
    _show(panel, "Errors only")
    panel._save()
    out = tmp_path / "saved.log"
    writes[0](str(out))
    saved = out.read_text(encoding="utf-8").splitlines()
    assert len(saved) == 4 and all(" ERR  " in line for line in saved)
    assert saved[1].endswith("Traceback (most recent call last):")


def test_every_line_of_a_command_trace_stays_hidden_by_default(panel):
    # the engine's trace of a failed command, with its multi-line stderr
    panel.append("[DEBUG]   -> shell: exit 1 (0.10s): first line\nsecond line")
    assert _levels(panel) == ["DEBUG", "DEBUG"]
    assert _shown(panel) == []
    _show(panel, "Verbose (adb commands)")
    assert len(_shown(panel)) == 2


def test_lines_of_an_untagged_message_are_read_one_by_one(panel):
    panel.append("Opening 'Pixel'…\n$ adb -s R58M123 shell ls")
    assert _levels(panel) == ["INFO", "DEBUG"]
    panel.append("[OK] Saved\n[WARNING] but slowly\nit took 9 s")
    assert _levels(panel)[2:] == ["OK", "WARNING", "WARNING"]


def test_a_silent_choice_that_cannot_be_saved_says_so(panel, monkeypatch):
    from turboadb.gui import settings

    def unwritable(_key, _value):
        raise OSError("settings.json could not be read (sharing violation)")

    monkeypatch.setattr(settings, "set", unwritable)
    panel.chk_silent.setChecked(not panel.chk_silent.isChecked())
    assert _levels(panel) == ["WARNING"]
    assert _shown(panel)[0].endswith(
        "Could not save the Silent choice: settings.json could not be read (sharing violation)")


def test_a_cancelled_step_reads_the_same_in_the_panel_and_the_toast(panel, monkeypatch):
    from turboadb.gui import fileutil
    from turboadb.gui.log_panel import classify
    from turboadb.gui.main_window import MainWindow

    assert classify("[CANCELLED] pull a.txt") == ("INFO", "Cancelled: pull a.txt", "CANCELLED")
    assert classify("[canceled] push b.txt")[:2] == ("INFO", "Cancelled: push b.txt")
    assert classify("$ adb devices") == ("DEBUG", "$ adb devices", None)
    assert classify("Rebooting…") == ("INFO", "Rebooting…", None)

    toasts, status = [], []
    monkeypatch.setattr(fileutil, "activity_toast",
                        lambda _parent, message, **kw: toasts.append((kw.get("level"), message)))
    window = types.SimpleNamespace(
        log_panel=panel, _log_dock=types.SimpleNamespace(isVisible=lambda: False),
        _show_log_dock=lambda: None,
        statusBar=lambda: types.SimpleNamespace(showMessage=lambda text, _ms: status.append(text)),
    )
    MainWindow._log(window, "[CANCELLED] pull a.txt")
    assert _shown(panel)[0].endswith(" info Cancelled: pull a.txt")  # no "[CANCELLED]" left
    assert toasts == [("info", "Cancelled: pull a.txt")]
    assert status == ["Cancelled: pull a.txt"]
