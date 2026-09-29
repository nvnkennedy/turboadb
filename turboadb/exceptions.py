"""Exception hierarchy for turboadb. Catch ADBError for everything."""

from __future__ import annotations


class ADBError(Exception):
    """Base class for all errors raised by this package."""


class ADBNotFoundError(ADBError):
    """The adb (or scrcpy) executable could not be located on the system.

    ``configured`` is the path that was set on purpose (``adb_path``,
    ``--adb-path``, ``TURBOADB_ADB``…) when it is that path that names no
    executable, or None when none could be found at all."""

    def __init__(self, *args, configured=None):
        super().__init__(*args)
        self.configured = configured


class ADBConnectionError(ADBError):
    """A device could not be reached / `adb connect` failed / no device online."""


class ADBTimeoutError(ADBError):
    """An adb operation exceeded its allotted time.

    ``result`` is the :class:`~turboadb.results.CommandResult` of what a
    one-shot command printed before it was stopped (exit code -1), or None."""

    def __init__(self, *args, result=None):
        super().__init__(*args)
        self.result = result


class ADBNotConnectedError(ADBError):
    """An operation needing a live device was attempted before connecting.

    Raised when adb itself reports the target as not connected — a stale serial,
    or a network device nobody ran ``adb connect`` for (see
    :meth:`~turboadb.core.ADBHandler.wait_for_device`).
    """


class ADBCommandError(ADBError):
    """An adb command exited non-zero while check=True."""

    def __init__(self, command: str, result):
        self.command = command
        self.result = result
        stderr = getattr(result, "stderr", "") or ""
        exit_code = getattr(result, "exit_code", "?")
        super().__init__(
            f"Command failed (exit={exit_code}): {command!r}\nstderr: {stderr.strip()[:500]}"
        )


class ADBTransferError(ADBError):
    """An adb push/pull upload or download failed."""


class ADBInstallError(ADBError):
    """An APK install/uninstall failed."""


class ScrcpyError(ADBError):
    """Launching or driving scrcpy (screen mirroring) failed."""
