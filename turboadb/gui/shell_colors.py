"""What the terminal colours by itself: device prompts and logcat lines.

A device shell on a terminal prints its own plain prompt (``[N|]host:/path $``)
and ``adb logcat`` prints plain lines, so everything used to come out in one
grey.  The console asks this module where a prompt's parts are and which
priority a logcat line has, and paints them in the theme's colours.  Pure
Python with no Qt import, so it is tested on its own.

Only lines that clearly are logcat lines count: each of logcat's formats
(threadtime, time, brief, tag, process, thread and long, with the uid,
year, usec/nsec, zone, epoch and monotonic variants) has its own exact
shape, so ``ls -l``, ``dmesg`` or an ``I/O error:`` message is left alone.
"""

from __future__ import annotations

import re
from typing import List, Optional, Tuple

# logcat's priority letters (A: an assert, from older Androids).
LEVELS = "VDIWEFA"

_L = f"[{LEVELS}]"
# month-day time with milli, micro or nano seconds, an optional year and zone;
# or seconds since the epoch / boot (-v epoch, -v monotonic)
_STAMP = r"(?:(?:\d{4}-)?\d\d-\d\d \d\d:\d\d:\d\d\.\d{3,9}(?: [+-]\d{4})?| *\d+\.\d{3,9})"
# -v uid puts "name:" or "10123:" (%5s:) in front of the pid
_UID = r"(?: *[^\s:()]+:)?"
# a pid or tid, which logcat prints as %5d
_ID = r"(?: {4}\d| {3}\d\d| {2}\d{3}| \d{4}|\d{5,})"
_LOGCAT_LINE = re.compile(
    # threadtime: 09-29 16:42:56.210  5927  5927 I Finsky  : message
    rf"{_STAMP} {_UID}{_ID} {_ID} (?P<threadtime>{_L}) "
    # time: 09-29 16:42:56.210 I/Finsky  ( 5927): message
    rf"|{_STAMP} (?P<time>{_L})/[^\n]*?\({_UID}{_ID}\): "
    # long: [ 09-29 16:42:56.210  5927: 5927 I/Finsky  ]  (the message follows)
    rf"|\[ {_STAMP} {_UID}{_ID}:{_ID} (?P<long>{_L})/[^\n]* \]$"
    # brief: I/Finsky  ( 5927): message
    rf"|(?P<brief>{_L})/[^\n]*?\({_UID}{_ID}\): "
    # thread: I( 5927: 5927) message
    rf"|(?P<thread>{_L})\({_UID}{_ID}:{_ID}\) "
    # process: I( 5927) message  (Finsky)
    rf"|(?P<process>{_L})\({_UID}{_ID}\) "
    # tag: I/Finsky  : message (a one-word tag, padded to 8: checked below)
    rf"|(?P<tag>{_L})/[^\s:()]+ *: "
)


def _match(line: str):
    match = _LOGCAT_LINE.match(line)
    if match is not None and match.lastgroup == "tag" and match.end() - 4 < 8:
        return None  # "E/x: …": logcat pads the tag to 8 characters
    return match


def logcat_level(line: str) -> Optional[str]:
    """The priority letter of *line* when it is a logcat line, else None."""
    match = _match(line)
    if match is None:
        return None
    return match.group(match.lastgroup)


def is_long_header(line: str) -> bool:
    """Whether *line* opens an entry of ``logcat -v long`` (its message lines
    follow, up to an empty line, and share its priority)."""
    match = _match(line)
    return match is not None and match.lastgroup == "long"


class LogcatLines:
    """Follows logcat lines one complete line at a time: :meth:`level` gives
    a line's priority, carrying a ``-v long`` header's to its message lines."""

    def __init__(self):
        self._carry = None

    def reset(self) -> None:
        self._carry = None

    def level(self, line: str) -> Optional[str]:
        match = _match(line)
        if match is not None:
            level = match.group(match.lastgroup)
            self._carry = level if match.lastgroup == "long" else None
            return level
        if self._carry is not None:
            if not line.strip():
                self._carry = None  # the empty line that ends a long entry
                return None
            return self._carry
        return None


# A device shell prompt at the start of a line: mksh's "[N|]host:/path $ "
# ("#" as root), with user@ in front on some builds, busybox's "/path # ", a
# bare "$ ".  Whatever follows the marker and its space is the command.
_PROMPT = re.compile(
    r"(?P<status>\d+\|)?"
    r"(?:(?P<user>[^\s@:/|$#]+@)?(?P<host>[^\s@:/|$#]+)(?P<colon>:))?"
    r"(?P<path>[/~][^\n]*?)?"
    r" ?(?P<mark>[$#])(?= |$)"
)
PROMPT_PARTS = ("status", "user", "host", "colon", "path", "mark")


def prompt_spans(line: str) -> Optional[List[Tuple[int, int, str]]]:
    """``(start, end, part)`` for each part of the prompt that starts *line*
    (see :data:`PROMPT_PARTS`), or None when the line starts with none.  The
    marker's part is ``"mark"`` for ``$`` and ``"root"`` for ``#``."""
    match = _PROMPT.match(line)
    if match is None:
        return None
    spans = []
    for part in PROMPT_PARTS:
        if match.group(part):
            name = part
            if part == "mark" and match.group(part) == "#":
                name = "root"
            spans.append((match.start(part), match.end(part), name))
    return spans
