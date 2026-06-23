"""Exception hierarchy for turboadb. Catch ADBError for everything."""

from __future__ import annotations


class ADBError(Exception):
    """Base class for all errors raised by this package."""


class ADBNotFoundError(ADBError):
    """The adb (or scrcpy) executable could not be located on the system."""


class ADBConnectionError(ADBError):
    """A device could not be reached / `adb connect` failed / no device online."""


class ADBTimeoutError(ADBError):
    """An adb operation exceeded its allotted time."""


class ADBNotConnectedError(ADBError):
    """An operation needing a live device was attempted before connecting."""


class ADBCommandError(ADBError):
    """An adb command exited non-zero while check=True."""

    def __init__(self, command: str, result):
        self.command = command
        self.result = result
        stderr = getattr(result, "stderr", "") or ""
        exit_code = getattr(result, "exit_code", "?")
        super().__init__(
            f"Command failed (exit={exit_code}): {command!r}\n"
            f"stderr: {stderr.strip()[:500]}"
        )


class ADBTransferError(ADBError):
    """An adb push/pull upload or download failed."""


class ADBInstallError(ADBError):
    """An APK install/uninstall failed."""


class ScrcpyError(ADBError):
    """Launching or driving scrcpy (screen mirroring) failed."""
