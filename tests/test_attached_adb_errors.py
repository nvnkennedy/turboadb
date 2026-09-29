"""A command attached to the console (``turboadb shell -- …``, ``adb -- …``,
the interactive ``turboadb shell``) reports an adb that cannot run as an
ADBError, like every other engine call.

``run_attached`` turned only a missing adb into ADBError: one that antivirus
had locked or quarantined (PermissionError, WinError 225) escaped as a bare
OSError past a caller's ``except ADBError``."""
import pytest

import turboadb.core as core
from turboadb.config import ADBConfig
from turboadb.core import ADBHandler
from turboadb.exceptions import ADBError


def _raising(error):
    def call(cmd, **_kw):
        raise error
    return call


@pytest.mark.parametrize("error", [
    FileNotFoundError(2, "The system cannot find the file specified"),
    PermissionError(13, "Access is denied"),
    OSError(22, "Operation did not complete successfully because the file contains a virus"),
])
def test_an_attached_command_with_an_adb_that_cannot_run_is_an_adb_error(
        fake_adb, monkeypatch, error):
    monkeypatch.setattr(core.subprocess, "call", _raising(error))
    handler = ADBHandler(ADBConfig(serial="R58M1"))
    with pytest.raises(ADBError, match="not runnable"):
        handler.run_attached(["shell", "ls"])
    with pytest.raises(ADBError, match="not runnable"):
        handler.interactive_shell("ls")


def test_an_attached_command_returns_adbs_exit_code(fake_adb, monkeypatch):
    seen = []

    def call(cmd, **kw):
        seen.append((list(cmd), kw))
        return 3

    monkeypatch.setattr(core.subprocess, "call", call)
    handler = ADBHandler(ADBConfig(serial="R58M1"))
    assert handler.run_attached(["shell", "false"]) == 3
    argv, kw = seen[0]
    assert argv[-4:] == ["-s", "R58M1", "shell", "false"] and kw == {"timeout": None}
