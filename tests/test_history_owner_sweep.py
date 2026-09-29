"""The clean-up at start leaves the history files of a TurboADB that still runs.

Every start removed the empty and the three-day-old ``turboadb-*.log`` files
in ~/.turboadb/logs, whoever wrote them: a second TurboADB deleted the live
history of the first (a file just started is empty, and a quiet terminal's
older part is closed and can be days old).  A history file now names its
owner, which holds a lock while it runs; the old rule applies only to files
whose owner is gone, and to those of older versions, which name none.
"""
import os
import time

import pytest

pytest.importorskip("PyQt5")

from turboadb.gui import app as app_mod  # noqa: E402
from turboadb.gui import scrollback  # noqa: E402
from turboadb.gui.settings import _lock_acquire, _lock_release  # noqa: E402

_OTHER = "4242-a1b2c3"


@pytest.fixture
def logs(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    folder = tmp_path / ".turboadb" / "logs"
    folder.mkdir(parents=True)
    return folder


def _history(folder, owner, tag, text="", days=0):
    path = folder / f"turboadb-{owner}-{tag}.log"
    path.write_text(text, encoding="utf-8")
    if days:
        old = time.time() - days * 86400
        os.utime(str(path), (old, old))
    return path


def test_the_history_of_an_instance_that_still_runs_is_left_alone(logs):
    held = _lock_acquire(str(logs / f".turboadb-{_OTHER}.lock"), 0)  # it runs
    assert held is not None
    try:
        started = _history(logs, _OTHER, "k2j4h6g8")  # empty: a history just begun
        quiet = _history(logs, _OTHER, "m1n3b5v7", "older part\n", days=5)
        app_mod._sweep_stale_logs()
        assert started.exists() and quiet.exists()
    finally:
        _lock_release(held)  # it has ended (or crashed)
    app_mod._sweep_stale_logs()
    assert not started.exists() and not quiet.exists()
    assert os.listdir(str(logs)) == []  # its lock file goes once its files have


def test_a_gone_instances_recent_history_is_kept_for_a_while(logs):
    recent = _history(logs, _OTHER, "p0o9i8u7", "its last output\n")
    (logs / f".turboadb-{_OTHER}.lock").write_text("")
    app_mod._sweep_stale_logs()
    assert recent.exists()
    assert (logs / f".turboadb-{_OTHER}.lock").exists()  # kept while its history is


def test_this_instances_own_history_is_never_swept(logs, qapp):
    from PyQt5.QtWidgets import QPlainTextEdit

    sb = scrollback.Scrollback(QPlainTextEdit())
    sb._MEMORY_LIMIT = 8  # spill to the history file at once
    try:
        sb.archive("output " * 20 + "\n")
        assert sb._flush(10)
        (path,) = [p for p in logs.iterdir() if p.suffix == ".log"]
        assert scrollback.history_owner(path.name) == scrollback.OWNER
        old = time.time() - 5 * 86400
        os.utime(str(path), (old, old))  # quiet for days
        app_mod._sweep_stale_logs()
        assert path.exists()
        assert scrollback.owner_running(str(logs), scrollback.OWNER)
    finally:
        sb.close()


def test_the_owner_lock_is_let_go_on_the_way_out(logs, monkeypatch):
    monkeypatch.setattr(scrollback, "_claimed", {})  # this test's own claims
    scrollback._claim(str(logs))
    assert scrollback.owner_running(str(logs), scrollback.OWNER)
    scrollback._release_claims()  # what exiting runs
    assert not scrollback.owner_running(str(logs), scrollback.OWNER)


def test_the_owner_is_read_from_the_name():
    assert scrollback.history_owner(f"turboadb-{_OTHER}-k2j4h6g8.log") == _OTHER
    assert scrollback.history_owner("turboadb-k2j4h6g8.log") is None  # an older version's
    assert scrollback.history_owner("turboadb-shell-20260927-101010.log") is None
