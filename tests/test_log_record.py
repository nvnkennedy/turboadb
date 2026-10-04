"""The log panel's record holds every line its view still shows.

"Save log…" and a change of "Show" work from the record.  It was one list of
20,000 entries for every level, so the raw adb trace (hidden by default, and
far more frequent than everything else) filled it, while the view kept 80,000
lines: an error still on screen was missing from the saved log, the one that
gets attached to a bug report, and changing "Show" emptied the view."""

import pytest

pytest.importorskip("PyQt5")

from turboadb.gui import log_panel  # noqa: E402

_ERROR = "Could not pull cat.png: remote object does not exist"


@pytest.fixture
def make_panel(qapp):
    made = []

    def make():
        widget = log_panel.LogPanel()
        made.append(widget)
        return widget

    yield make
    for widget in made:
        widget.deleteLater()


def _messages(lines):
    """The text after the time and the level badge ("12:00:00 info …")."""
    return [line[14:] for line in lines]


def _shown(panel):
    return _messages(panel.view.toPlainText().splitlines())


def _show(panel, label):
    panel.level_box.setCurrentIndex([name for name, _rank in log_panel._FILTERS].index(label))


def _saved(panel, monkeypatch, tmp_path):
    from turboadb.gui import fileutil

    writes = []
    monkeypatch.setattr(fileutil, "save_output",
                        lambda _parent, _title, _name, write, **_kw: writes.append(write))
    panel._save()
    out = tmp_path / "saved.log"
    writes[0](str(out))
    return _messages(out.read_text(encoding="utf-8").splitlines())


def _trace(panel, count, first=0):
    for start in range(first, first + count, 500):
        panel.append("\n".join(f"$ adb -s R58M123 shell ls /sdcard/{n}"
                               for n in range(start, min(start + 500, first + count))))


def test_the_hidden_trace_does_not_push_an_error_out_of_the_saved_log(
        make_panel, monkeypatch, tmp_path):
    panel = make_panel()
    panel.append(f"[ERROR] {_ERROR}")
    _trace(panel, 21000)
    assert _shown(panel) == [_ERROR]  # the trace is hidden: the error is still on screen
    assert _saved(panel, monkeypatch, tmp_path) == [_ERROR]
    _show(panel, "Errors only")
    assert _shown(panel) == [_ERROR]
    _show(panel, "Normal")
    assert _shown(panel) == [_ERROR]
    # with the trace shown, its newest lines follow in the order they came
    _show(panel, "Verbose (adb commands)")
    assert _shown(panel)[-1] == "$ adb -s R58M123 shell ls /sdcard/20999"
    saved = _saved(panel, monkeypatch, tmp_path)
    assert saved[0] == _ERROR and saved[-1] == "$ adb -s R58M123 shell ls /sdcard/20999"


def test_many_messages_do_not_push_an_older_error_out_either(make_panel, monkeypatch, tmp_path):
    """Under "Errors only" the view keeps an error however many messages came
    after it, so the record has to as well."""
    panel = make_panel()
    _show(panel, "Errors only")
    panel.append(f"[ERROR] {_ERROR}")
    for start in range(0, 21000, 500):
        panel.append("\n".join(f"[OK] step {n} done" for n in range(start, start + 500)))
    assert _shown(panel) == [_ERROR]
    assert _saved(panel, monkeypatch, tmp_path) == [_ERROR]
    _show(panel, "Warnings + Errors")
    assert _shown(panel) == [_ERROR]


def test_the_view_shows_no_line_the_record_has_dropped(make_panel, monkeypatch, tmp_path):
    monkeypatch.setattr(log_panel, "_KEPT_PER_LEVEL", 40)
    panel = make_panel()
    for i in range(100):
        panel.append(f"[INFO] step {i}")
        panel.append(f"$ adb shell step {i}")
    shown = _shown(panel)
    assert shown == [f"step {i}" for i in range(60, 100)]
    assert _saved(panel, monkeypatch, tmp_path) == shown  # every line on screen is saved
    _show(panel, "Verbose (adb commands)")
    assert len(_shown(panel)) == 40 and _shown(panel)[-2:] == ["step 99", "$ adb shell step 99"]
    _show(panel, "Normal")
    assert _shown(panel) == shown  # and comes back after a change of "Show"
    panel.append("[ERROR] the one error")
    for i in range(100, 160):
        panel.append(f"[INFO] step {i}")
    assert _shown(panel) == [f"step {i}" for i in range(120, 160)]  # the view's own limit
    saved = _saved(panel, monkeypatch, tmp_path)
    assert saved == ["the one error"] + [f"step {i}" for i in range(120, 160)]


def test_each_level_keeps_its_own_newest_lines_in_the_order_they_came(make_panel, monkeypatch):
    monkeypatch.setattr(log_panel, "_KEPT_PER_LEVEL", 3)
    panel = make_panel()
    panel.append("[OK] one")
    panel.append("$ adb devices")
    panel.append("[ERROR] three\nits second line")
    panel.append("[WARNING] four")
    assert [(level, message) for _time, level, message in panel._entries] == [
        ("OK", "one"), ("DEBUG", "$ adb devices"), ("ERROR", "three"),
        ("ERROR", "its second line"), ("WARNING", "four")]
    for i in range(5):
        panel.append(f"$ adb shell step {i}")
    panel.append("[INFO] six")
    assert [message for _time, _level, message in panel._entries] == [
        "one", "three", "its second line", "four",
        "$ adb shell step 2", "$ adb shell step 3", "$ adb shell step 4", "six"]
    assert len(panel._entries) == 8
    panel._clear()
    assert len(panel._entries) == 0 and list(panel._entries) == []
