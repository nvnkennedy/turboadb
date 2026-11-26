"""Exception hierarchy for turboadb. Catch ADBError for everything."""

from __future__ import annotations

import re

# What adb says about the device it was asked for, as one kind (see
# device_problem).  Checked in this order: the first that matches is the kind.
_PROBLEMS = (
    ("more-than-one", re.compile(r"more than one (?:device|emulator)", re.I)),
    ("no-permissions", re.compile(
        r"\bno permissions\b|insufficient permissions for device", re.I)),
    ("unauthorized", re.compile(
        r"\bunauthori[sz]ed\b|\b(?:still )?authorizing\b|allow usb debugging", re.I)),
    ("not-found", re.compile(
        r"device (?:'[^'\n]*'|\"[^\"\n]*\"|\S+) not found|\bdevice not found\b", re.I)),
    ("no-device", re.compile(r"no devices?(?:/emulators?)? (?:found|connected)", re.I)),
    ("offline", re.compile(r"\bdevice (?:is )?offline\b|^\s*offline\s*$", re.I | re.M)),
)

DEVICE_PROBLEMS = ("no-device", "not-found", "unauthorized", "offline", "more-than-one",
                   "no-permissions")


def device_problem(text) -> str:
    """The kind of device problem adb's text *text* reports, or ``""``:

    * ``"no-device"``: no device at all (``no devices/emulators found``)
    * ``"not-found"``: the serial asked for is not listed
    * ``"unauthorized"``: the device waits for "Allow USB debugging" on its
      screen (also while it is still authorizing)
    * ``"offline"``: listed, but not answering
    * ``"more-than-one"``: several devices and no serial given
    * ``"no-permissions"``: this PC's user may not use the USB device (Linux udev)

    ``unauthorized`` and ``more-than-one`` say nothing about whether the
    device is on: treat them as "can't tell", never as "the device is off"."""
    if not text:
        return ""
    text = str(text)
    for kind, pattern in _PROBLEMS:
        if pattern.search(text):
            return kind
    return ""


class ADBError(Exception):
    """Base class for all errors raised by this package."""

    @property
    def device_problem(self) -> str:
        """The device problem this error reports (see :func:`device_problem`),
        from its message and the output of the command it carries."""
        parts = [str(self)]
        result = getattr(self, "result", None)
        for name in ("stderr", "stdout"):
            value = getattr(result, name, None)
            if isinstance(value, str):
                parts.append(value)
        return device_problem("\n".join(parts))


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
    """An adb operation exceeded its allotted time: its adb client was ended.

    ``result`` is the :class:`~turboadb.results.CommandResult` of what the
    command printed before it was ended (``timed_out`` True, exit code 124),
    or None.  ``timed_out`` is always True."""

    timed_out = True
    cancelled = False

    def __init__(self, *args, result=None):
        super().__init__(*args)
        self.result = result


class ADBDeviceTimeoutError(ADBTimeoutError, ADBConnectionError):
    """A wait for the device ran out of time (``wait_for_device``, the wait
    of ``connect``).  Also an ADBConnectionError, which it was before 6.5.0."""


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


class ADBCancelledError(ADBConnectionError, ADBTransferError):
    """The call was cancelled (its ``cancel=``): its adb client was ended at
    once.  ``result`` is the :class:`~turboadb.results.CommandResult` of what
    it printed until then (``cancelled`` True, exit code 124), or None.

    Also an ADBConnectionError and an ADBTransferError: a cancelled connect or
    shell raised the one and a cancelled push or pull the other before 6.5.0."""

    timed_out = False
    cancelled = True

    def __init__(self, *args, result=None):
        super().__init__(*args)
        self.result = result


class ADBInstallError(ADBError):
    """An APK install/uninstall failed."""


class ScrcpyError(ADBError):
    """Launching or driving scrcpy (screen mirroring) failed."""
