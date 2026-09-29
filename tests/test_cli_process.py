"""The CLI process around the command: `turboadb gui` hands over before the
CLI's logging and tool fetch (the GUI has its own of both), a process without a
stderr gets no stderr log handler, and a second Ctrl+C during `record` is never
stuck behind an uninterruptible wait."""
import logging
import sys
import threading

import pytest

import turboadb.cli as cli

from test_cli_commands import FakeDev


def _cli_handlers():
    return [h for h in logging.getLogger("turboadb").handlers if getattr(h, "_turboadb_cli", False)]


@pytest.fixture
def no_cli_handler():
    """Detach the CLI's stderr handler an earlier test may have attached."""
    logger = logging.getLogger("turboadb")
    saved = _cli_handlers()
    for handler in saved:
        logger.removeHandler(handler)
    yield
    for handler in _cli_handlers():
        logger.removeHandler(handler)
    for handler in saved:
        logger.addHandler(handler)


def test_gui_starts_without_the_cli_log_handler_or_a_tool_fetch(monkeypatch, no_cli_handler):
    """The GUI then echoed every event of its session to the console (and under
    pythonw failed on each), and a blocking adb-only fetch before its window
    kept its own fetch from getting scrcpy."""
    import turboadb.toolsdl as toolsdl

    fetched, launched = [], []
    monkeypatch.setattr(cli, "adb_available", lambda *a, **k: False)
    monkeypatch.setattr(toolsdl, "ensure_tools", lambda **kw: fetched.append(kw))
    monkeypatch.setattr(cli, "launch_gui", lambda argv=None: launched.append(argv) or 0)
    assert cli.main(["gui"]) == 0
    assert launched == [[]] and fetched == [] and _cli_handlers() == []

    assert cli.main(["targets", "list", "--json"]) == 0
    assert len(_cli_handlers()) == 1  # an ordinary command still logs to stderr


def test_no_stderr_means_no_stderr_handler(monkeypatch, no_cli_handler):
    monkeypatch.setattr(sys, "stderr", None)  # pythonw -m turboadb serve at login
    cli._configure_logging()
    assert _cli_handlers() == []


def test_a_second_ctrl_c_during_record_is_never_stuck(monkeypatch, capsys):
    """After the first Ctrl+C, a join without a timeout waited for the recording
    to be pulled; on Windows such a wait can't be interrupted."""
    release = threading.Event()

    def screen_record(path, **kw):
        release.wait(30)  # a slow pull that ignores the stop request
        return path

    real_join = threading.Thread.join
    timeouts = []

    def join(self, timeout=None):
        if self.name == "turboadb-record" and len(timeouts) < 2:
            timeouts.append(timeout)
            raise KeyboardInterrupt  # Ctrl+C, twice
        return real_join(self, timeout)

    monkeypatch.setattr(threading.Thread, "join", join)
    monkeypatch.setattr(cli, "adb_available", lambda *a, **k: True)
    monkeypatch.setattr(cli, "_handler", lambda args, **kw: FakeDev(screen_record=screen_record))
    try:
        assert cli.main(["record", "clip.mp4"]) == 130
    finally:
        release.set()
    assert timeouts == [0.2, 0.2]
    assert "Stopping the recording" in capsys.readouterr().err
