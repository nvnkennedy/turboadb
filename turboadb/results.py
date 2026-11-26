"""Rich, structured result objects returned by every action."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field, asdict, fields
from typing import Optional, Union


# ANSI/VT escape sequences (CSI like ESC[1;32m, OSC like ESC]0;title BEL, etc.)
# OSC and CSI must be tried BEFORE the two-byte forms: the old class
# ``[@-Z\\-_]`` also matched ``]`` (0x5D), so "ESC ]" was consumed alone and the
# OSC payload (e.g. a window title) leaked into the cleaned text.
# Then ESC, intermediate bytes, final byte: the charset choice ESC ( B ends
# terminfo's sgr0 (top, vi), and without it the ESC went and "(B" stayed.
# Last the two-byte forms, including ESC 7 / ESC 8 (save / restore the cursor)
# and ESC = / ESC > (keypad modes), which left "7", "=" and the like behind.
_ANSI_RE = re.compile(
    r"\x1b(?:\][^\x07\x1b]*(?:\x07|\x1b\\)|\[[0-?]*[ -/]*[@-~]|[ -/]+[0-~]|[0-Z\\^-~])"
)
# control chars except tab(09), newline(0a); carriage-return(0d) handled separately
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def strip_ansi(text: str) -> str:
    """Remove ANSI/VT escape codes, carriage returns, and other control chars so
    streamed logcat / shell output is clean to save, match, and read."""
    if not text:
        return text
    text = _ANSI_RE.sub("", text)
    text = text.replace("\r", "")
    return _CTRL_RE.sub("", text)


def human_bytes(num) -> str:
    """``1536`` -> ``"1.5 KB"``, ``500`` -> ``"500 B"``; ``None`` -> ``"—"``.

    The one size format: the CLI, TransferResult, the Files tab and the
    Transfers panel all print a size the same way."""
    if num is None:
        return "—"
    num = max(0.0, float(num))
    if num < 1024:
        return f"{int(num)} B"
    for unit in ("KB", "MB", "GB", "TB"):
        num /= 1024.0
        if num < 1024 or unit == "TB":
            return f"{num:.1f} {unit}"
    return f"{num:.1f} TB"  # pragma: no cover - the loop always returns


# The exit code of a command TurboADB ended: it ran past its time limit, or
# its call was cancelled (turboadb.adbproc.TIMEOUT_EXIT_CODE).
TIMEOUT_EXIT_CODE = 124


@dataclass
class CommandResult:
    """Result of an adb / adb-shell command execution.

    ``timed_out`` / ``cancelled``: TurboADB ended the command, at its time
    limit or because the call was cancelled; ``exit_code`` is then 124
    (:data:`TIMEOUT_EXIT_CODE`)."""

    command: str
    exit_code: int
    stdout: Union[str, bytes]
    stderr: str
    duration: float
    device: str = ""
    started_at: float = field(default_factory=time.time)
    timed_out: bool = False
    cancelled: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0

    @property
    def device_problem(self) -> str:
        """The device problem the command's output reports (see
        :func:`turboadb.exceptions.device_problem`), or ``""``."""
        from .exceptions import device_problem

        out = self.stdout if isinstance(self.stdout, str) else ""
        return "" if self.ok else device_problem(self.stderr + chr(10) + out)

    @property
    def text(self) -> str:
        """stdout with surrounding whitespace/newlines stripped - print-ready."""
        if isinstance(self.stdout, bytes):
            return self.stdout.decode("utf-8", errors="replace").strip()
        return self.stdout.strip()

    @property
    def lines(self) -> list:
        """stdout split into non-empty stripped lines."""
        raw = (
            self.stdout.decode("utf-8", errors="replace")
            if isinstance(self.stdout, bytes)
            else self.stdout
        )
        return [ln for ln in raw.splitlines() if ln.strip()]

    def __bool__(self) -> bool:
        return self.ok

    def as_dict(self) -> dict:
        # dataclasses.asdict() deep-copies every field, and stdout can be
        # megabytes of screencap or logcat.  No field here is a nested
        # dataclass, so a shallow mapping is identical and free.  timed_out
        # and cancelled are there only when set: the usual document (the
        # CLI's --json) keeps the keys it always had.
        found = {f.name: getattr(self, f.name) for f in fields(self)}
        for flag in ("timed_out", "cancelled"):
            if not found[flag]:
                del found[flag]
        return found

    def __str__(self) -> str:
        status = "ok" if self.ok else f"FAILED (exit {self.exit_code})"
        return f"$ {self.command}  [{status}, {self.duration:.2f}s]"


@dataclass
class TransferResult:
    """Result of a file/dir transfer (push/pull)."""

    source: str
    dest: str
    direction: str  # "push" or "pull"
    size_bytes: int
    duration: float
    files: int = 1

    @property
    def speed_bps(self) -> float:
        return self.size_bytes / self.duration if self.duration > 0 else 0.0

    @property
    def human_speed(self) -> str:
        return f"{human_bytes(self.speed_bps)}/s"

    @property
    def human_size(self) -> str:
        return human_bytes(self.size_bytes)

    def as_dict(self) -> dict:
        d = asdict(self)
        d.update(speed_bps=self.speed_bps, human_speed=self.human_speed)
        return d

    def __str__(self) -> str:
        verb = "Pushed" if self.direction == "push" else "Pulled"
        files = f"{self.files} files, " if self.files != 1 else ""
        return (
            f"{verb} {self.source} -> {self.dest}  "
            f"({files}{self.human_size} in {self.duration:.2f}s, "
            f"{self.human_speed})"
        )


@dataclass
class StreamResult:
    """Result of a streaming operation (logcat / live shell), returned when it
    ends (stop_event, timeout, stop-on-match, or EOF).

    ``exit_code`` is the command's exit status when it ended by itself, 124
    (:data:`TIMEOUT_EXIT_CODE`) when its *timeout* or its *cancel* ended it
    (``timed_out`` / ``cancelled`` say which), and None when it was stopped
    otherwise (its stop_event, stop-on-match, the consumer).  ``stderr`` is
    adb's error text when the stream kept it apart from the lines (logcat
    does)."""

    lines: int
    matches: list
    saved_to: Optional[str] = None
    exit_code: Optional[int] = None
    stderr: str = ""
    timed_out: bool = False
    cancelled: bool = False

    @property
    def matched(self) -> bool:
        return bool(self.matches)

    @property
    def failed(self) -> bool:
        """The command ended by itself with a non-zero exit status."""
        return self.exit_code not in (None, 0) and not (self.timed_out or self.cancelled)

    def __bool__(self) -> bool:
        return self.matched

    def __str__(self) -> str:
        return (
            f"<StreamResult lines={self.lines} matches={len(self.matches)} "
            f"saved_to={self.saved_to!r}>"
        )


@dataclass
class OperationResult:
    """
    Returned by safe-mode operations. Wraps a value or an error so GUI handlers
    never have to try/except. Falsy on failure.
    """

    success: bool
    action: str = ""
    value: object = None
    error: Optional[Exception] = None

    def __bool__(self) -> bool:
        return self.success

    @property
    def ok(self) -> bool:
        return self.success

    @property
    def timed_out(self) -> bool:
        """The operation ran past its time limit (its error is an
        ADBTimeoutError, or its result says so)."""
        return bool(getattr(self.error, "timed_out", False)
                    or getattr(self.value, "timed_out", False))

    @property
    def cancelled(self) -> bool:
        """The operation was cancelled (its error is an ADBCancelledError, or
        its result says so)."""
        return bool(getattr(self.error, "cancelled", False)
                    or getattr(self.value, "cancelled", False))

    @property
    def device_problem(self) -> str:
        """The device problem the failure reports (see
        :func:`turboadb.exceptions.device_problem`), or ``""``."""
        problem = getattr(self.error, "device_problem", "")
        if problem:
            return problem
        return getattr(self.value, "device_problem", "") if not self.success else ""

    @property
    def stdout(self) -> str:
        if isinstance(self.value, CommandResult):
            return str(self.value.stdout)
        if isinstance(self.value, str):
            return self.value
        return ""

    @property
    def stderr(self) -> str:
        if isinstance(self.value, CommandResult):
            return str(self.value.stderr)
        return str(self.error or "")

    @property
    def text(self) -> str:
        if isinstance(self.value, CommandResult):
            return self.value.text
        return str(self.value or "")

    def unwrap(self):
        """Return value on success, else re-raise the captured error."""
        if self.success:
            return self.value
        raise self.error if self.error else RuntimeError(f"{self.action} failed")

    def __str__(self) -> str:
        if self.success:
            return f"<OperationResult {self.action} ok value={self.value!r}>"
        return f"<OperationResult {self.action} error={self.error!r}>"
