"""What the terminal colours by itself: what output means, device prompts and
logcat lines.

Colour is kept for what matters.  In plain output only the words that say
something went wrong (an error, "Permission denied", "blocked"), needs
attention (a warning) or worked ("Success", "3 files pushed") are coloured
(:func:`meaning_spans`); a listing or a file's text stays in the default
colour.  A device shell on a terminal prints its own plain prompt
(``[N|]host:/path $``), which the console draws in one quiet colour, red
where it means something (a failed command's status, root's ``#``).  Of
``adb logcat``'s lines only warnings, errors and fatal ones are coloured.
The console asks this module where these are, and paints them in the
theme's colours.  Pure Python with no Qt import, so it is tested on its own.

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


# logcat's priorities that are coloured in a terminal: the rest are routine
COLOURED_LEVELS = "WEFA"


# ------------------------------------------------------------------- meaning
# The words that say what happened, as the terminal colours them: something
# failed or was refused, needs attention, or worked.  Matched as words of
# their own and in any case, the longest phrase first: "not found" is a
# failure (not "found"), "0 errors" a success (not "errors").
MEANING_KINDS = ("error", "warning", "success")
_ERROR_PHRASES = (
    "permission denied", "permission denial", "access denied", "access is denied",
    "operation not permitted", "not permitted", "read-only file system",
    "no such file or directory", "no such file", "no such device", "no such process",
    "no space left on device", "out of memory", "is a directory", "not a directory",
    "directory not empty", "resource busy",
    "does not exist", "not found", "not recognized", "not recognised", "not installed",
    "not connected", "not allowed", "not authorized", "not responding", "unknown command",
    "unknown option", "connection refused", "connection reset", "no route to host",
    "network is unreachable", "unreachable", "timed out", "offline", "unauthorized",
    "unauthorised", "forbidden", "denied", "blocked", "refused", "rejected", "fail", "fails",
    "failed", "failure", "failures", "fatal", "error", "errors", "exception", "cannot",
    "can't", "could not", "couldn't", "unable to", "invalid", "illegal",
    "segmentation fault", "core dumped", "aborted", "crashed", "killed", "panic",
)
_WARNING_PHRASES = (
    "warning", "warnings", "warn", "deprecated", "retrying", "not supported",
    "unsupported", "disconnected",
)
_SUCCESS_PHRASES = (
    "success", "successful", "successfully", "succeeded", "0 errors", "no errors",
    "without errors", "without error", "0 failed", "0 failures", "0 warnings", "no warnings",
    "connected to", "already connected to", "completed", "done", "installed", "passed",
    "pushed", "pulled", "copied",
)


def _trie(phrases) -> str:
    """A pattern for any of *phrases* (lower case; a space in one matches any
    blanks) as a tree of their letters: a line is read letter by letter, not
    phrase by phrase, which keeps a flood of output fast.  Where one phrase
    goes on from another, the longer one is tried first."""
    heads = {}
    for phrase in phrases:
        if phrase:
            heads.setdefault(phrase[0], []).append(phrase[1:])
    if not heads:
        return ""
    branches = [(r"\s+" if head == " " else re.escape(head)) + _trie(tails)
                for head, tails in sorted(heads.items())]
    body = branches[0] if len(branches) == 1 else "(?:" + "|".join(branches) + ")"
    return f"(?:{body})?" if "" in phrases else body


# what each phrase says (see MEANING_KINDS)
_KIND = {phrase: kind for kind, phrases in (
    ("error", _ERROR_PHRASES), ("warning", _WARNING_PHRASES), ("success", _SUCCESS_PHRASES),
) for phrase in phrases}

# A word of its own: not part of another word, an identifier, a path, a file
# name or an option ("mFailed", "fail_count", "/data/fail", "error.log",
# "--no-error", "non-fatal"), and not a setting's name ("granted=true").
_BEFORE = r"(?<![\w/\\.@$-])"
_AFTER = r"(?![\w/\\@$=-])(?!\.\w)"
_MEANING = re.compile(
    rf"{_BEFORE}(?:(?P<phrase>{_trie(_KIND)})|(?-i:(?P<ok>OK))){_AFTER}"
    # errors by name, in the case they are written in ("onError" is none): an
    # exception's class (IOException, java.lang.OutOfMemoryError) and a
    # package manager's failure (INSTALL_FAILED_VERSION_DOWNGRADE)
    r"|(?-i:(?<![\w.$])(?:[a-z_$][\w$]*\.)*[A-Z][\w$]*(?:Exception|Error)\b(?!\.\w)"
    r"|\b[A-Z]+(?:_[A-Z]+)*_FAILED(?:_[A-Z0-9]+)*\b)",
    re.IGNORECASE,
)

# Words too common in output to tell a line apart: a phrase is looked for by
# one of its other words ("no such device" by "such").
_COMMON = frozenset((
    "access", "already", "connection", "device", "does", "file", "network", "operation",
    "process", "resource", "unknown", "without",
))


def _triggers(phrases) -> Tuple[str, ...]:
    """A word of each phrase, the longest one that is not common, leaving out
    those another one is part of ("errors" is found by looking for "error")."""
    words = set()
    for phrase in phrases:
        split = phrase.lower().split()
        words.add(max([w for w in split if w not in _COMMON] or split, key=len))
    return tuple(sorted(w for w in words if not any(o != w and o in w for o in words)))


# A line is searched only when one of these is in it (lower-cased): most
# output says none of it, so a flood of text or a long listing costs little.
_TRIGGERS = _triggers(_ERROR_PHRASES + _WARNING_PHRASES + _SUCCESS_PHRASES)
_TRIGGER = re.compile(_trie(_TRIGGERS))
_LONGEST = 4096  # a longer line is not searched (a binary dump, minified text)


def may_mean(line: str) -> bool:
    """Whether *line* may have words :func:`meaning_spans` finds: a quick look
    that rules out most output without searching it."""
    if len(line) > _LONGEST:
        return False
    return "OK" in line or _TRIGGER.search(line.lower()) is not None


def meaning_spans(line: str) -> List[Tuple[int, int, str]]:
    """``(start, end, kind)`` of the words in *line* that say something went
    wrong or was refused (``"error"``), needs attention (``"warning"``) or
    worked (``"success"``), in order.  See :data:`MEANING_KINDS`."""
    if not line or not may_mean(line):
        return []
    spans = []
    for match in _MEANING.finditer(line):
        phrase = match.group("phrase")
        if phrase is not None:
            kind = _KIND[" ".join(phrase.lower().split())]
        else:
            kind = "success" if match.group("ok") else "error"
        spans.append((match.start(), match.end(), kind))
    return spans


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
