"""The engine calls that start adb and read it while it runs (a push or pull,
logcat and other streams, an interactive shell, the screen capture stream, a
recording) report an adb that cannot be started as an ADBError, like every
one-shot command.

They let the OSError through: code that followed "catch ADBError for
everything" missed an adb that antivirus had locked or quarantined, or one
that was being replaced by an update."""
import pytest

import turboadb.core as core
from turboadb.config import ADBConfig
from turboadb.core import ADBHandler
from turboadb.exceptions import ADBError

_CANNOT_START = [
    FileNotFoundError(2, "The system cannot find the file specified"),
    PermissionError(13, "Access is denied"),
    OSError(8, "%1 is not a valid Win32 application"),
]


def _push(handler, tmp_path):
    source = tmp_path / "a.txt"
    source.write_bytes(b"a")
    return handler.push(str(source), "/sdcard/a.txt")


_STARTS_ADB = {
    "push": _push,
    "pull": lambda handler, tmp_path: handler.pull("/sdcard/a.txt", str(tmp_path / "a.txt")),
    "logcat": lambda handler, tmp_path: handler.logcat(dump=True),
    "stream": lambda handler, tmp_path: handler.stream(["shell", "getevent"]),
    "iter_lines": lambda handler, tmp_path: list(handler.iter_lines(["shell", "getevent"])),
    "open_shell": lambda handler, tmp_path: handler.open_shell(),
    "popen": lambda handler, tmp_path: handler.popen(["logcat"]),
    "open_screencap_stream": lambda handler, tmp_path: handler.open_screencap_stream(),
    "screen_record": lambda handler, tmp_path: handler.screen_record(str(tmp_path / "c.mp4")),
}


@pytest.fixture
def unstartable(fake_adb, monkeypatch):
    """Make every start of an adb child fail with the given error."""

    def with_error(error):
        def popen(cmd, **_kw):
            raise error

        monkeypatch.setattr(core.subprocess, "Popen", popen)

    return with_error


@pytest.mark.parametrize("call", sorted(_STARTS_ADB))
@pytest.mark.parametrize("error", _CANNOT_START, ids=lambda error: type(error).__name__)
def test_an_adb_that_cannot_be_started_is_an_adb_error(unstartable, tmp_path, call, error):
    unstartable(error)
    handler = ADBHandler(ADBConfig(serial="R58M1"))
    with pytest.raises(ADBError, match="adb executable not runnable") as raised:
        _STARTS_ADB[call](handler, tmp_path)
    assert raised.value.__cause__ is error  # what the system said is kept


def test_safe_mode_hands_the_same_error_back(unstartable, tmp_path):
    unstartable(_CANNOT_START[1])
    handler = ADBHandler(ADBConfig(serial="R58M1"), safe=True)
    result = _STARTS_ADB["pull"](handler, tmp_path)
    assert not result.success and isinstance(result.error, ADBError)
    assert "Access is denied" in str(result.error)
