"""Live logcat viewer: level + tag + app filters on the device, a regex filter
and highlight here, pause, clear, save.

The reader thread archives every line it receives BEFORE anything is dropped,
so Save and the filter always have the whole capture.  The filter runs on that
thread too, ahead of the on-screen caps, so those caps only ever bound matching
lines; a new filter searches the complete archive (off the UI thread once it
has spilled to disk), not just what happens to be on screen.
"""

from __future__ import annotations

import collections
import logging
import re
import subprocess
import threading
import time
from collections import deque

from PyQt5.QtCore import QThread, pyqtSignal, QTimer, Qt
from PyQt5.QtGui import QFont, QTextCursor, QTextCharFormat, QColor
from PyQt5.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QPushButton,
    QLineEdit,
    QComboBox,
    QCheckBox,
    QLabel,
    QPlainTextEdit,
    QMenu,
)

from ..core import ADBHandler
from . import icons, theme, settings as settings_mod
from .icons import icon
from .qtutil import (
    close_jobs, disconnect_signals, page_toolbar, park_thread, run_job, thread_running,
)
from .scrollback import Scrollback

try:  # the regex parser, to spot patterns that could hang the matcher
    from re import _constants as _sre_constants, _parser as _sre_parse  # Python 3.11+
except ImportError:  # Python 3.8-3.10
    try:
        import sre_constants as _sre_constants
        import sre_parse as _sre_parse
    except ImportError:  # pragma: no cover - neither: patterns are not vetted
        _sre_constants = _sre_parse = None

_log = logging.getLogger(__name__)

# Level picker entries: (label, icon, tone). The tone matches the level's
# colour family in the log view, so the picker reads like a legend.
_LEVELS = (
    ("Verbose (all)", "list", "dim"),
    ("Debug", "bug", "blue"),
    ("Info", "info", "green"),
    ("Warn", "alert", "amber"),
    ("Error", "x", "red"),
    ("Fatal", "zap", "purple"),
)
_LEVEL_LETTER = {"Verbose": "V", "Debug": "D", "Info": "I", "Warn": "W", "Error": "E", "Fatal": "F"}

_HINT_START = "Press Start to stream the device log"
_HINT_DUMP = "Press Dump to print the log buffer once"
_HINT_WAIT = "Waiting for log output…"
_HINT_SEARCH = "Searching the whole capture…"
_HINT_NO_MATCH = "No lines match the filter"

# The level letter of a line in every format Settings offers: threadtime
# ("… 1234  5678 E Tag: …"), time, brief and tag ("E/Tag…") and long's header
# ("[ … 1234: 5678 E/Tag ]").  Only the start of a line is searched.
_LEVEL_RE = re.compile(r"(?:^|\s)([VDIWEF])[\s/]")
# The timestamp that starts a threadtime or time line, or long's header: the
# point logcat's -T can resume from.
_STAMP_RE = re.compile(r"^(?:\[ +)?(\d\d-\d\d \d\d:\d\d:\d\d\.\d+)")
# The logcat of an older Android has no -T: it stops at once and prints its
# usage, after getopt's own complaint ("logcat: unknown option -- T").
_NO_TAIL_OPTION_RE = re.compile(r"(?:unknown|invalid|illegal) option -- '?T'?(?:\W|$)")
_USAGE_RE = re.compile(r"Unrecognized Option", re.IGNORECASE)
# long's entry header: "[ 09-27 12:00:00.123  1234: 5678 I/Tag ]".
_LONG_HEADER_RE = re.compile(r"^\[ +\d\d-\d\d \d\d:\d\d:\d\d\.\d+ ")
# Lines about the capture rather than from it: TurboADB's own notes, and adb's
# and logcat's errors (which old devices print on stdout).  A filter never
# hides them.
_MARK = "[TurboADB] "
_STATUS_RE = re.compile(r"^(?:\[TurboADB\] |error: |adb: |logcat: |- waiting for |read: unexpected EOF)")


def _stamp_before(stamp: str, point: str) -> bool:
    """Whether the logcat time *stamp* is earlier than *point*.  logcat writes
    no year: a January time is later than a December one (the year turned)."""
    if point[:2] == "12" and stamp[:2] == "01":
        return False
    return stamp < point


_REPEATS = {getattr(_sre_constants, name) for name in ("MAX_REPEAT", "MIN_REPEAT")
            if hasattr(_sre_constants, name)}
# Python 3.11+ only: possessive repeats (``C++``) and atomic groups.
_ATOMIC = {getattr(_sre_constants, name) for name in ("POSSESSIVE_REPEAT", "ATOMIC_GROUP")
           if hasattr(_sre_constants, name)}


def _regex_risk(items, outer=None) -> str:
    """Why the parsed pattern *items* should not run as a regex ("" if fine).

    *outer* tells whether the nearest enclosing repeat (of more than one) is
    unbounded.  A repeat nested in another with either one unbounded, like
    ``(a+)+`` or ``(\\w+\\s?)*``, can backtrack for minutes on one long line."""
    for op, av in items:
        if op in _ATOMIC:
            return "atomic"
        found = ""
        if op in _REPEATS:
            _low, high, sub = av
            if high > 1:
                unbounded = high == _sre_constants.MAXREPEAT
                if outer is not None and (outer or unbounded):
                    return "nested"
                found = _regex_risk(sub, unbounded)
            else:
                found = _regex_risk(sub, outer)
        elif op == _sre_constants.SUBPATTERN:
            found = _regex_risk(av[-1], outer)
        elif op == _sre_constants.BRANCH:
            found = next((r for r in (_regex_risk(alt, outer) for alt in av[1]) if r), "")
        elif op in (_sre_constants.ASSERT, _sre_constants.ASSERT_NOT):
            found = _regex_risk(av[1], outer)
        elif op == _sre_constants.GROUPREF_EXISTS:
            found = _regex_risk(av[1], outer) or (_regex_risk(av[2], outer) if av[2] else "")
        if found:
            return found
    return ""


# Characters the overlap check below tries: ASCII and a few others, plus every
# character a pattern names (and the ends of its ranges), so two sets overlap
# for the check exactly when they share one of these.
_SAMPLE = frozenset(map(chr, range(128))) | frozenset("\u00a0\u00e9\u00c9\u00df\u0660\u2028\u4e2d")
_END = "end of the pattern"  # in a follow set: nothing has to come next


def _category_res():
    """The regex classes behind the parser's CATEGORY codes."""
    if _sre_constants is None:
        return {}
    names = {"DIGIT": r"\d", "NOT_DIGIT": r"\D", "SPACE": r"\s", "NOT_SPACE": r"\S",
             "WORD": r"\w", "NOT_WORD": r"\W"}
    return {getattr(_sre_constants, "CATEGORY_" + name): re.compile(expr)
            for name, expr in names.items() if hasattr(_sre_constants, "CATEGORY_" + name)}


_CATEGORIES = _category_res()


class _Overlaps:
    """Choices inside a repeat that can go on with the same character.

    Python's matcher tries each way to split a line among the iterations of
    a repeat, so a repeat whose body can match the same text in two ways
    (``(a|aa)+``, ``(a|b|ab)*``, ``(a?a)+``, ``(.|\\s)+``) backtracks for
    minutes on one long line that almost matches.  Such a body has a choice
    (an alternation, or an optional part) whose ways can start with the same
    character: the FIRST sets overlap, with what may come next (FOLLOW)
    standing in for a way that matches nothing.  Alternatives that the parser
    turns into one character class (``(a|b)+``, ``(\\w|\\d)+``) and choices that
    differ in their first character (``(error|warn)+``, ``(ab|ac)+``) are
    fine."""

    def __init__(self, parsed, ignore_case: bool):
        self.ic = ignore_case
        chars = set(_SAMPLE)
        self._collect(parsed, chars)
        if ignore_case:
            chars |= {x for c in chars for x in (c.lower(), c.upper()) if len(x) == 1}
        self.alphabet = frozenset(chars)

    def _collect(self, items, chars) -> None:
        """Add the characters *items* names, and the ends of its ranges."""
        C = _sre_constants
        for op, av in items:
            if op in (C.LITERAL, C.NOT_LITERAL):
                chars.add(chr(av))
            elif op == C.IN:
                for iop, iav in av:
                    if iop in (C.LITERAL, C.NOT_LITERAL):
                        chars.add(chr(iav))
                    elif iop == C.RANGE:
                        chars.update((chr(iav[0]), chr(iav[1])))
            elif op == C.BRANCH:
                for alt in av[1]:
                    self._collect(alt, chars)
            elif op in _REPEATS or op in _ATOMIC or op in (C.ASSERT, C.ASSERT_NOT):
                self._collect(av[-1], chars)
            elif op == C.SUBPATTERN:
                self._collect(av[-1], chars)
            elif op == C.GROUPREF_EXISTS:
                self._collect(av[1], chars)
                if av[2]:
                    self._collect(av[2], chars)

    # ---- the characters one item can start with ----
    def _same(self, c, code) -> bool:
        return ord(c) == code or (self.ic and c.lower() == chr(code).lower())

    def _in_range(self, c, low, high) -> bool:
        if low <= ord(c) <= high:
            return True
        return self.ic and any(
            len(x) == 1 and low <= ord(x) <= high for x in (c.lower(), c.upper())
        )

    def _charset(self, op, av) -> frozenset:
        C = _sre_constants
        alphabet = self.alphabet
        if op == C.LITERAL:
            return frozenset(c for c in alphabet if self._same(c, av))
        if op == C.NOT_LITERAL:
            return frozenset(c for c in alphabet if not self._same(c, av))
        if op == C.ANY:
            return alphabet
        if op != C.IN:
            return alphabet  # anything else: assume the worst
        negate = bool(av) and av[0][0] == C.NEGATE
        found = set()
        for iop, iav in av[1:] if negate else av:
            if iop == C.LITERAL:
                found.update(c for c in alphabet if self._same(c, iav))
            elif iop == C.NOT_LITERAL:
                found.update(c for c in alphabet if not self._same(c, iav))
            elif iop == C.RANGE:
                found.update(c for c in alphabet if self._in_range(c, *iav))
            elif iop == C.CATEGORY and iav in _CATEGORIES:
                found.update(c for c in alphabet if _CATEGORIES[iav].match(c))
            else:
                return alphabet
        return frozenset(alphabet - found if negate else found)

    def first(self, items):
        """``(characters, nullable)``: what *items* can start with, and
        whether it can match nothing at all."""
        C = _sre_constants
        chars = set()
        for op, av in items:
            if op in (C.LITERAL, C.NOT_LITERAL, C.ANY, C.IN):
                chars |= self._charset(op, av)
                return chars, False
            if op == C.BRANCH:
                nullable = False
                for alt in av[1]:
                    alt_chars, alt_nullable = self.first(alt)
                    chars |= alt_chars
                    nullable = nullable or alt_nullable
                if not nullable:
                    return chars, False
            elif op in _REPEATS or op in _ATOMIC:
                sub_chars, sub_nullable = self.first(av[-1])
                chars |= sub_chars
                if op in _REPEATS and av[0] > 0 and not sub_nullable:
                    return chars, False
            elif op == C.SUBPATTERN:
                sub_chars, sub_nullable = self.first(av[-1])
                chars |= sub_chars
                if not sub_nullable:
                    return chars, False
            elif op == C.GROUPREF_EXISTS:
                yes_chars, yes_nullable = self.first(av[1])
                no_chars, no_nullable = self.first(av[2]) if av[2] else (set(), True)
                chars |= yes_chars | no_chars
                if not (yes_nullable or no_nullable):
                    return chars, False
            elif op == C.GROUPREF:
                chars |= self.alphabet  # whatever the group matched, maybe nothing
            # AT and the lookarounds match no character
        return chars, True

    def _following(self, items, index, follow):
        rest, nullable = self.first(items[index + 1:])
        return rest | follow if nullable else rest

    # ---- the check ----
    def found(self, items, follow=frozenset((_END,))) -> bool:
        """Whether a repeat in *items* has a choice whose ways overlap."""
        C = _sre_constants
        for index, (op, av) in enumerate(items):
            if op in (C.LITERAL, C.NOT_LITERAL, C.ANY, C.IN, C.AT):
                continue  # no choice here (and a long plain filter costs nothing)
            after = self._following(items, index, follow)
            if op in _REPEATS and av[1] > 1:
                body = av[2]
                again, _nullable = self.first(body)
                if self._choices_overlap(body, again | after):
                    return True
            elif op in _REPEATS or op == C.SUBPATTERN or op in _ATOMIC:
                if self.found(av[-1], after):
                    return True
            elif op == C.BRANCH:
                if any(self.found(alt, after) for alt in av[1]):
                    return True
            elif op == C.GROUPREF_EXISTS:
                if self.found(av[1], after) or (av[2] and self.found(av[2], after)):
                    return True
        return False

    def _path(self, items, follow):
        """``(charsets, plain)``: the characters *items* matches at positions
        0, 1, ... while it is a plain run of single characters.  *plain*: it
        ends there, and *follow* comes next; otherwise the last charset is
        what may start where it stops being plain, and anything after it."""
        C = _sre_constants
        flat = []
        stack = list(reversed(items))
        while stack:  # a group is just its content here
            op, av = stack.pop()
            if op == C.SUBPATTERN:
                stack.extend(reversed(av[-1]))
            else:
                flat.append((op, av))
        path = []
        for index, (op, av) in enumerate(flat):
            if op in (C.LITERAL, C.NOT_LITERAL, C.ANY, C.IN):
                path.append(self._charset(op, av))
            elif op not in (C.AT, C.ASSERT, C.ASSERT_NOT):  # (these match no character)
                rest, nullable = self.first(flat[index:])
                path.append(frozenset(rest | follow if nullable else rest))
                return path, False
        return path, True

    @staticmethod
    def _paths_meet(a, b, follow) -> bool:
        """Whether two alternatives (:meth:`_path`) can match the same text:
        at every position until one of them is no longer known, some
        character suits both.  ``bar`` and ``baz`` part at their third."""
        (path_a, plain_a), (path_b, plain_b) = a, b
        for index in range(max(len(path_a), len(path_b)) + 1):
            chars = []
            for path, plain in ((path_a, plain_a), (path_b, plain_b)):
                if index < len(path):
                    chars.append(path[index])
                elif plain and index == len(path):
                    chars.append(follow)
                else:
                    return True  # unknown from here: assume they can
            if not (chars[0] & chars[1]):
                return False
        return True

    def _choices_overlap(self, items, follow) -> bool:
        """Whether a choice in *items* (a repeat's body, followed by *follow*)
        has two ways on with the same text."""
        C = _sre_constants
        for index, (op, av) in enumerate(items):
            if op in (C.LITERAL, C.NOT_LITERAL, C.ANY, C.IN, C.AT):
                continue
            after = self._following(items, index, follow)
            if op == C.BRANCH:
                paths = [self._path(alt, after) for alt in av[1]]
                for number, path in enumerate(paths):
                    if any(self._paths_meet(path, other, after) for other in paths[number + 1:]):
                        return True
                if any(self._choices_overlap(alt, after) for alt in av[1]):
                    return True
            elif op in _REPEATS:
                if av[0] == 0:  # optional: taken or skipped, then the same next character
                    chars, _nullable = self.first(av[2])
                    if chars & after:
                        return True
                if self._choices_overlap(av[2], after):
                    return True
            elif op == C.SUBPATTERN or op in _ATOMIC:
                if self._choices_overlap(av[-1], after):
                    return True
            elif op == C.GROUPREF_EXISTS:
                if self._choices_overlap(av[1], after) or (
                        av[2] and self._choices_overlap(av[2], after)):
                    return True
        return False


# The lines a pattern is timed on (see _too_slow): a long logcat line, a long
# run of word characters (a hex dump, a base64 blob) and a line of punctuation
# and spaces.  Each shape stalls other patterns, and none holds the words a
# filter looks for, so the search fails and backtracks as far as it can.
_PROBE_LENGTH = 512
_PROBE_LINES = tuple(
    (text * (_PROBE_LENGTH // len(text) + 1))[:_PROBE_LENGTH]
    for text in (
        "09-27 12:00:00.123  1234  5678 W ActivityManager: Slow operation: 123ms so far, "
        "now at startProcess: done updating battery stats, package=com.example.app/.Main, "
        "uid=10123 pid=4567 reason=start-activity ",
        "abcdefghij0123456789_",
        "-.:;,/=+()[]{}<>'\"!?#&* ",
    )
)
# A pattern that takes longer than this to search one of them is too slow.
# Ordinary ones (Exception, .*Exception.*, ActivityManager|WindowManager) need
# a few milliseconds at most; repeats in a row need tens or thousands.
_PROBE_BUDGET_S = 0.010


def _too_slow(pattern) -> bool:
    """Whether searching a probe line with *pattern* takes longer than
    ``_PROBE_BUDGET_S``.

    Repeats one after another, such as ``\\w+.*\\w+Exception``, ``.* .*timeout``
    or ``.*.*x``, pass the checks above but try every way to split a line
    among them: milliseconds per line, a second per render tick of the
    Highlight and minutes for a view of it, on the UI thread, or on the reader
    thread for a Filter, which holds the GIL for every search.  Whatever form
    such a pattern takes, timing it catches it.  The lines are searched at
    growing lengths, so a pattern that is slow on short ones is refused before
    a long one takes seconds, and a slow search is timed once more before it
    counts, so a moment's stall of the process does not refuse an ordinary
    pattern."""
    clock = time.perf_counter
    length = 32
    while True:
        for line in _PROBE_LINES:
            text = line[:length]
            for _attempt in range(2):
                started = clock()
                pattern.search(text)
                spent = clock() - started
                if spent <= _PROBE_BUDGET_S or spent > 10 * _PROBE_BUDGET_S:
                    break
            if spent > _PROBE_BUDGET_S:
                return True
        if length >= _PROBE_LENGTH:
            return False
        length = min(2 * length, _PROBE_LENGTH)


def compile_filter(text: str, match_case: bool = False):
    """``(pattern, note)`` for the Filter or Highlight box; ``(None, "")`` when empty.

    Text that isn't a usable regular expression is matched as plain text
    instead, and *note* says why — so ``*crash*`` or ``onCreate(`` still
    filter rather than silently showing everything.  Python's matcher never
    releases the GIL, so a pattern that backtracks catastrophically would
    freeze the whole window whichever thread ran it: nested repeats, and a
    repeat whose body can match the same text in two ways (see
    :class:`_Overlaps`), are matched as plain text too.  So are possessive
    repeats and atomic groups, which exist only on Python 3.11+ (``C++`` then
    means something else), and any pattern that takes too long to search a
    long line (see :func:`_too_slow`)."""
    if not text:
        return None, ""
    flags = 0 if match_case else re.IGNORECASE
    try:
        pattern = re.compile(text, flags)
    except Exception:  # re.error, OverflowError, RecursionError
        note = "Not a valid regular expression, so it is matched as plain text."
    else:
        try:
            parsed = _sre_parse.parse(text, flags) if _sre_parse is not None else None
            risk = _regex_risk(parsed) if parsed is not None else ""
            if not risk and parsed is not None:
                state = getattr(parsed, "state", None)
                ignore_case = bool((flags | getattr(state, "flags", 0)) & re.IGNORECASE)
                if _Overlaps(parsed, ignore_case).found(parsed):
                    risk = "overlap"
        except Exception:
            risk = ""
        if not risk and _too_slow(pattern):
            risk = "slow"
        if not risk:
            return pattern, ""
        if risk == "nested":
            note = ("Nested repeats such as (a+)+ can take minutes to match, "
                    "so this is matched as plain text.")
        elif risk == "overlap":
            note = ("A repeat whose alternatives can match the same text, such as "
                    "(a|aa)+, can take minutes to match, so this is matched as plain text.")
        elif risk == "slow":
            note = ("This pattern takes too long to search a long line (repeats in a row, "
                    "such as .* .*x, try every way to split it), so it is matched as "
                    "plain text.")
        else:
            note = "Possessive repeats and atomic groups are matched as plain text."
    return re.compile(re.escape(text), flags), note


class _Entries:
    """Group 'long' format lines into whole entries — the ``[ header ]`` line,
    the message lines and the blank line that ends it — so a filter keeps or
    hides an entry as a whole instead of splitting its header from its text.

    *text* maps an item to its line (items are lines unless given)."""

    def __init__(self, text=None):
        self.partial = []
        self._text = text or (lambda item: item)

    def feed(self, items) -> list:
        """The entries *items* complete (lists of items); the rest waits."""
        done, cur = [], self.partial
        for item in items:
            line = self._text(item)
            if cur and _LONG_HEADER_RE.match(line):
                done.append(cur)
                cur = [item]
                continue
            cur.append(item)
            if line == "":
                done.append(cur)
                cur = []
        self.partial = cur
        return done

    def flush(self) -> list:
        rest, self.partial = self.partial, []
        return [rest] if rest else []


def _kept(keep, line) -> bool:
    return keep is None or bool(keep(line)) or _STATUS_RE.match(line) is not None


def _search_lines(lines, keep, stop, limit, entries=False, partial=False, cancelled=None,
                  first=0):
    """``(found, total)``: the last *limit* lines *keep* accepts among the first
    *stop* of *lines*, as ``(index, line)``, and how many it accepted in all.

    With *entries* ('long' format) whole entries are kept or dropped, and with
    *partial* (the capture still runs) an unfinished last entry is left for
    the live stream, which delivers it once complete.  None when *cancelled*
    says a newer search replaced this one.  *first* is the index of the first
    of *lines* (older ones went with the history limit)."""
    found = deque(maxlen=max(1, limit))
    total = 0
    group = _Entries(text=lambda item: item[1]) if entries and keep is not None else None
    for index, line in enumerate(lines, first):
        if index >= stop:
            break
        if cancelled is not None and not index & 0xFFF and cancelled():
            return None
        if group is None:
            if _kept(keep, line):
                found.append((index, line))
                total += 1
            continue
        for entry in group.feed(((index, line),)):
            if any(_kept(keep, text) for _i, text in entry):
                found.extend(entry)
                total += len(entry)
    if group is not None and not partial:
        for entry in group.flush():
            if any(_kept(keep, text) for _i, text in entry):
                found.extend(entry)
                total += len(entry)
    return list(found), total


def terminal_icon_pixmap(name: str, size: int, tone: str, ratio: float = 0.0):
    """An icon pixmap for the terminal-coloured surfaces (log view, video well).

    Those surfaces keep the same dark colours in every theme, so the icon uses
    the dark-theme hue of *tone* and never needs a refresh on theme switches.
    *ratio* is the device pixel ratio (default: the application's).
    """
    from PyQt5.QtGui import QGuiApplication, QPixmap

    if ratio <= 0:
        app = QGuiApplication.instance()
        ratio = app.devicePixelRatio() if app is not None else 1.0
    pm = QPixmap(int(round(size * ratio)), int(round(size * ratio)))
    pm.setDevicePixelRatio(ratio)
    pm.fill(Qt.transparent)
    try:
        from PyQt5.QtCore import QByteArray, QRectF
        from PyQt5.QtGui import QPainter
        from PyQt5.QtSvg import QSvgRenderer
    except ImportError:  # QtSvg missing: an empty pixmap keeps the layout intact
        return pm
    renderer = QSvgRenderer(QByteArray(icons.svg(name, theme.hue(tone, "dark")).encode("utf-8")))
    if renderer.isValid():
        painter = QPainter(pm)
        painter.setRenderHint(QPainter.Antialiasing)
        renderer.render(painter, QRectF(0, 0, size, size))
        painter.end()
    return pm


class _EmptyLogHint(QWidget):
    """Centred icon + hint over the empty log view (mouse passes through)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(10)
        self.glyph = QLabel()
        self.glyph.setAlignment(Qt.AlignCenter)
        self.glyph.setPixmap(terminal_icon_pixmap("logcat", 40, "amber"))
        self.label = QLabel(_HINT_START)
        self.label.setAlignment(Qt.AlignCenter)
        self.label.setObjectName("logcatEmptyHint")  # colour and size: theme.py
        v.addWidget(self.glyph)
        v.addWidget(self.label)

    def text(self) -> str:
        return self.label.text()

    def set_text(self, text: str) -> None:
        self.label.setText(text)


class _Capture:
    """The complete capture: every line in arrival order, archived with a
    running number, and the filter that lines arriving now are kept with.

    The reader thread adds lines while the UI thread swaps the filter, clears
    and searches, so both go through one lock: a batch is archived and given
    the filter of that moment in one step.  A filter change therefore splits
    the capture at one exact line — the archive search covers everything
    before it and the live stream everything after, nothing twice or missed.
    """

    def __init__(self, sb):
        self.sb = sb
        self.lock = threading.Lock()
        self.seq = 0  # lines archived since the panel opened (never goes back)
        self.base = 0  # seq of the archive's first line (Clear starts a new one)
        self.keep = None  # predicate for lines added from now on; None keeps all

    def add(self, lines, cancelled=None):
        """Archive *lines*: ``(seq of the first one, the filter to apply)``.

        Every line is archived (the numbering depends on it), so a reader
        thread passes *cancelled*: it then waits, before taking the lock,
        while the history writer is far behind (a disk slower than the log),
        instead of piling the log up in memory."""
        text = "\n".join(lines) + "\n"
        if cancelled is not None:
            self.sb.wait_for_room(len(text), cancelled)
        with self.lock:
            start = self.seq
            self.sb.archive(text, lossless=True)
            self.seq += len(lines)
            return start, self.keep

    def set_keep(self, keep):
        """Keep lines added from now on with *keep*: ``(seq, base)`` of that moment."""
        with self.lock:
            self.keep = keep
            return self.seq, self.base

    def reset(self) -> int:
        """Forget the archived lines (Clear); returns where the new archive starts."""
        with self.lock:
            self.sb.reset()
            self.base = self.seq
            return self.seq

    def position(self):
        with self.lock:
            return self.seq, self.base


class _LogcatThread(QThread):
    """Read ``adb logcat``: archive every line, filter it, and collect what is
    kept for the panel to pull.

    No per-batch cross-thread signal: over a slow/RDP link logcat floods
    thousands of lines/sec, and the panel's fixed-rate render timer drains
    :meth:`take` instead.  That also means a short burst followed by
    silence is shown promptly — a time-based batch only flushed when the
    *next* chunk arrived, so the last lines of a burst could wait forever.
    adb's own error output arrives on a separate pipe and is shown whatever
    the filter says.
    """

    _READ_SIZE = 65536
    # Kept lines held for the panel to pull.  The panel drains this every
    # render tick, but a flood while the UI is stalled (a modal dialog, a slow
    # RDP repaint, a hidden page) would otherwise grow it without limit.  The
    # oldest are dropped and counted — after they were archived, so Save and
    # a new filter still have them.
    _LINES_MAX = 20000
    _STDERR_KEEP = 20  # adb's last error lines, for the report when it ends
    _TAIL_KEEP = 200  # lines carrying the newest timestamp (a restart's overlap)

    def __init__(self, handler, args, clear_first: bool = False, *, capture=None,
                 package=None, skip_seen=None, long_entries: bool = False,
                 from_start: bool = False, backlog=None, dump_args=None):
        super().__init__()
        self.handler = handler
        self.args = list(args)
        self.clear_first = clear_first
        self.capture = capture  # _Capture: archive + current filter (None: keep all)
        self.package = package  # an app to resolve to --pid when starting
        # A logcat without -T (see LogcatPanel._tail_refused) streams the
        # device's whole buffer first (*from_start*).  *dump_args* (a -d of
        # the same filters) then reads that buffer once and its last *backlog*
        # lines are the capture's history (see _read_backlog); with a
        # *skip_seen* point, what came before it is left out (see _drop_seen).
        self.backlog = backlog
        self.dump_args = list(dump_args) if dump_args else None
        self._from_start = bool(from_start)
        self._reached = False
        self.proc = None
        self._stopping = False
        # The stream has ended (EOF, or stop() closed it); what is left is
        # reaping adb and reading its last error text, which can take seconds.
        self.ending = False
        self._lock = threading.Lock()
        self._entries_out = []  # [start seq, kept lines, dropped count] per batch
        self._size = 0
        self._entries = _Entries() if long_entries else None
        # How it ended, for the panel's report and a seamless restart.
        self.returncode = None
        self.error = ""
        self.stderr_lines = deque(maxlen=self._STDERR_KEEP)
        self.received = 0
        self.last_stamp = None
        self.last_at = 0.0
        self.tail = []
        # A restart resumes at the old capture's last timestamp, so logcat
        # repeats the lines that carry it: *skip_seen* = (stamp, those lines).
        stamp, seen = skip_seen or (None, ())
        self._seen = collections.Counter(seen) if seen else None
        self._seen_stamp = stamp

    # ---- hand-off to the panel ----
    def take(self) -> list:
        """Everything kept since the last call (thread-safe), as
        ``[start seq, lines, dropped]`` per batch."""
        with self._lock:
            out, self._entries_out, self._size = self._entries_out, [], 0
        return out

    def take_lines(self) -> list:
        """All kept lines since the last call (thread-safe)."""
        return [line for _start, lines, _dropped in self.take() for line in lines]

    def take_dropped(self) -> int:
        """How many kept lines were dropped since the last call (thread-safe)."""
        with self._lock:
            dropped = 0
            for entry in self._entries_out:
                dropped += entry[2]
                entry[2] = 0
        return dropped

    def _publish(self, lines, start: int = 0) -> None:
        with self._lock:
            self._entries_out.append([start, list(lines), 0])
            self._size += len(lines)
            overflow = self._size - self._LINES_MAX
            for entry in self._entries_out:
                if overflow <= 0:
                    break
                cut = min(overflow, len(entry[1]))
                del entry[1][:cut]
                entry[2] += cut
                self._size -= cut
                overflow -= cut

    # ---- one batch of lines ----
    def _ingest(self, lines, status: bool = False) -> None:
        """Archive *lines*, keep what the filter matches and hand that over.

        *status* lines (adb's errors, TurboADB's notes) skip the filter."""
        if not status and self._seen is not None:
            lines = self._drop_seen(lines)
            if not lines:
                return
        start, keep = (
            self.capture.add(lines, cancelled=lambda: self._stopping)
            if self.capture is not None else (0, None)
        )
        if not status:
            self.received += len(lines)
            self._note_stamp(lines)
            if self._entries is not None:
                done = self._entries.feed(lines)
                if keep is not None:
                    lines = [ln for entry in done if any(_kept(keep, x) for x in entry)
                             for ln in entry]
            elif keep is not None:
                lines = [ln for ln in lines if _kept(keep, ln)]
        if lines:
            self._publish(lines, start)

    def _finish_entries(self) -> None:
        """At EOF: the last 'long' entry is complete now."""
        if self._entries is None:
            return
        rest = self._entries.flush()
        keep = self.capture.keep if self.capture is not None else None
        if keep is None or not rest:
            return  # without a filter its lines went out as they arrived
        kept = [ln for entry in rest if any(_kept(keep, x) for x in entry) for ln in entry]
        if kept:
            self._publish(kept, self.capture.position()[0])

    def _note_stamp(self, lines) -> None:
        """Remember the newest timestamp and the lines that carry it (and any
        unstamped lines after them), so a restart can resume exactly there."""
        now = time.monotonic()
        last = None
        for i in range(len(lines) - 1, -1, -1):
            m = _STAMP_RE.match(lines[i])
            if m is not None:
                last = (i, m.group(1))
                break
        if last is None:
            # no timestamp (brief/tag format, or long's message lines)
            self.tail.extend(lines)
            if self.last_stamp is None:
                self.last_at = now
        else:
            i, stamp = last
            j = i
            while j > 0:
                m = _STAMP_RE.match(lines[j - 1])
                if m is not None and m.group(1) != stamp:
                    break
                j -= 1
            if j == 0 and stamp == self.last_stamp:
                self.tail.extend(lines)
            else:
                self.tail = list(lines[j:])
            self.last_stamp = stamp
            self.last_at = now
        if len(self.tail) > self._TAIL_KEEP:
            del self.tail[:-self._TAIL_KEEP]

    def _drop_seen(self, lines) -> list:
        """Leave out lines the previous capture already archived: resuming at
        its last timestamp makes logcat print the lines carrying it again.
        Without -T (*from_start*) the stream begins with the device's whole
        buffer, and the lines before that timestamp are left out as well."""
        out = []
        for index, line in enumerate(lines):
            seen = self._seen
            if seen is None:
                out.extend(lines[index:])
                break
            if line.startswith("--------- "):
                continue  # logcat's "beginning of <buffer>" banner on every start
            if self._seen_stamp is None:
                # no timestamps: only the first line can repeat (-T 1)
                if not seen.get(line):
                    out.append(line)
                self._seen = None
                continue
            m = _STAMP_RE.match(line)
            if self._from_start and not self._reached:
                if m is None or _stamp_before(m.group(1), self._seen_stamp):
                    continue  # the buffer before the resume point: archived already
                self._reached = True
            if m is not None and m.group(1) != self._seen_stamp:
                self._seen = None  # past the overlap
                out.append(line)
            elif seen.get(line, 0) > 0:
                seen[line] -= 1
            else:
                out.append(line)
        return out

    # ---- the reader ----
    def _spawn(self, args):
        try:
            return self.handler.popen(args, stderr="pipe")
        except TypeError as exc:
            if "stderr" not in str(exc):
                raise
            return self.handler.popen(args)  # a handler without it: merged, as before

    def _resolve(self, name):
        """The PID for the App box: a number as given, else the app's process."""
        if name.isdigit():
            return int(name)
        try:
            pid = self.handler.pid_of(name, safe=False)
        except Exception as exc:
            self.error = f"could not look up {name}: {exc}"
        else:
            if pid is not None:
                return pid
            self.error = f"{name} is not running on the device — start the app, then press Start"
        self._ingest([_MARK + self.error], status=True)
        return None

    def _read_backlog(self, args) -> None:
        """For a logcat without -T: read the device's buffer once (*args*, a
        ``-d`` dump), keep its last :attr:`backlog` lines as the history this
        capture starts with, and make the stream that follows, which begins
        with the whole buffer again, leave out everything up to where this
        read ended (see :meth:`_drop_seen`).  In a format without times there
        is no such point: the stream then shows the whole buffer."""
        try:
            proc = self.handler.popen(args)  # adb's errors inline, as the device's
        except Exception:
            return  # the stream after it says what is wrong
        with self._lock:
            self.proc = proc
            stopping = self._stopping
        if stopping:
            self._kill(proc)
            self._reap(proc)
            return
        read = getattr(proc.stdout, "read1", None) or proc.stdout.read
        chunks = []
        try:
            while True:
                chunk = read(self._READ_SIZE)
                if not chunk:
                    break
                chunks.append(chunk)
        except (OSError, ValueError):
            pass  # pipe closed by stop()
        self._reap(proc)
        with self._lock:
            if self.proc is proc:
                self.proc = None
        if self._stopping:
            return
        text = b"".join(chunks).decode("utf-8", "replace")
        lines = [ln.rstrip("\r") for ln in text.split("\n")]
        lines = [ln for ln in lines if ln.strip() and not ln.startswith("--------- ")]
        last = next((i for i in range(len(lines) - 1, -1, -1) if _STAMP_RE.match(lines[i])),
                    None)
        if last is None:
            return  # an empty buffer, or no times to find the end of this read by
        if self.backlog:
            self._ingest(lines[-int(self.backlog):])
        stamp = _STAMP_RE.match(lines[last]).group(1)
        first = last
        while first > 0:
            m = _STAMP_RE.match(lines[first - 1])
            if m is not None and m.group(1) != stamp:
                break
            first -= 1
        self._seen = collections.Counter(lines[first:])
        self._seen_stamp = stamp
        self._from_start = True
        self._reached = False

    def _read_stderr(self, pipe) -> None:
        read = getattr(pipe, "read1", None) or pipe.read
        partial = b""
        try:
            while True:
                chunk = read(4096)
                if not chunk:
                    break
                *done, partial = (partial + chunk).split(b"\n")
                self._stderr([ln.decode("utf-8", "replace").rstrip("\r") for ln in done])
        except (OSError, ValueError):
            pass  # pipe closed by stop()
        if partial:
            self._stderr([partial.decode("utf-8", "replace").rstrip("\r")])

    def _stderr(self, lines) -> None:
        lines = [ln for ln in lines if ln.strip()]
        if lines:
            self.stderr_lines.extend(lines)
            self._ingest(lines, status=True)

    def run(self):
        if self.clear_first and not self._stopping:
            try:
                self.handler.logcat_clear()
            except Exception as exc:  # capture still starts; just say why history remains
                _log.warning("logcat clear failed: %s", exc)
        if self._stopping:
            return
        args, dump_args = self.args, self.dump_args
        if self.package:
            pid = self._resolve(self.package)
            if pid is None or self._stopping:
                return
            args = args[:1] + [f"--pid={pid}"] + args[1:]  # Android 7+
            if dump_args:
                dump_args = dump_args[:1] + [f"--pid={pid}"] + dump_args[1:]
        if dump_args:
            self._read_backlog(dump_args)
            if self._stopping:
                return
        try:
            proc = self._spawn(args)
        except Exception as exc:
            self.error = str(exc) or type(exc).__name__
            self._ingest([f"{_MARK}logcat could not start: {self.error}"], status=True)
            return
        with self._lock:
            # stop() may have run while popen() was starting the process: it
            # saw no proc then, so honour it here instead of leaking adb logcat.
            self.proc = proc
            stopping = self._stopping
        if stopping:
            self._kill(proc)
            self._reap(proc)
            return
        err_pipe = getattr(proc, "stderr", None)
        err_reader = None
        if err_pipe is not None:
            err_reader = threading.Thread(
                target=self._read_stderr, args=(err_pipe,), name="turboadb-logcat-stderr",
                daemon=True,
            )
            err_reader.start()
        stdout = proc.stdout
        read = getattr(stdout, "read1", None) or stdout.read
        partial = []  # pieces of the current unterminated line (no re-splitting)
        try:
            while True:
                chunk = read(self._READ_SIZE)
                if not chunk:
                    break  # EOF or pipe closed by stop()
                nl = chunk.rfind(b"\n")
                if nl < 0:
                    partial.append(chunk)
                    continue
                partial.append(chunk[:nl])
                data = b"".join(partial)
                partial = [chunk[nl + 1:]] if nl + 1 < len(chunk) else []
                self._ingest(
                    [ln.decode("utf-8", "replace").rstrip("\r") for ln in data.split(b"\n")]
                )
        except (OSError, ValueError):
            pass  # pipe closed by stop()
        self.ending = True
        tail = b"".join(partial)
        if tail:
            self._ingest([tail.decode("utf-8", "replace").rstrip("\r")])
        self._finish_entries()
        self._reap(proc)
        if err_reader is not None:
            err_reader.join(1.0)  # adb's last words are usually why it ended

    def _reap(self, proc) -> None:
        try:
            self.returncode = proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            self._kill(proc)
            try:
                self.returncode = proc.wait(timeout=2.0)
            except Exception:
                pass
        except Exception:
            pass

    @staticmethod
    def _kill(proc) -> None:
        """Kill *proc* and close its pipes, so a blocking read returns at once.
        Never waits: the reader thread reaps it."""
        from ..proctree import stop_process

        stop_process(proc, grace=0, close_pipes=True, reap=False)

    def stop(self):
        # kill the process AND close the pipe so a blocking read returns at once
        with self._lock:
            self._stopping = True
            proc = self.proc
        if proc is not None:
            self._kill(proc)


class _ZoomEdit(QPlainTextEdit):
    """A read-only log view whose font zooms with Ctrl+wheel / Ctrl+± — the
    persisted size is shared with the terminal (same ``term_font_size``)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setReadOnly(True)

    def wheelEvent(self, event):
        if event.modifiers() & Qt.ControlModifier:
            self._bump(1 if event.angleDelta().y() > 0 else -1)
            event.accept()
            return
        super().wheelEvent(event)

    def keyPressEvent(self, event):
        m, k = event.modifiers(), event.key()
        if m & Qt.ControlModifier and k in (Qt.Key_Plus, Qt.Key_Equal):
            self._bump(1)
            return
        if m & Qt.ControlModifier and k == Qt.Key_Minus:
            self._bump(-1)
            return
        super().keyPressEvent(event)

    def _bump(self, step):
        f = self.font()
        current = f.pointSize() or settings_mod.DEFAULTS["term_font_size"]
        size = max(settings_mod.FONT_SIZE_MIN, min(settings_mod.FONT_SIZE_MAX, current + step))
        if size == current:
            return
        f.setPointSize(size)
        self.setFont(f)
        # one debounced, off-thread settings write per zoom gesture
        settings_mod.set_later("term_font_size", size)


class _Remark:
    """Lines of the Logcat view whose highlight marks the re-marker redoes
    (see LogcatPanel._remark_step)."""

    def __init__(self, pattern, olds, spans, whole):
        self.pattern = pattern  # the highlight to mark (None: marks only come off)
        self.olds = olds  # earlier highlights whose marks come off
        # [newest, oldest] line numbers (as drawn) still to do, the newest span first
        self.spans = spans
        # every line of the view: once done, only *pattern*'s marks are on screen
        self.whole = whole


class LogcatPanel(QWidget):
    log = pyqtSignal(str)

    _LEVEL_COLOR = theme.LOGCAT_LEVELS
    # At most this many lines are redrawn when the filter changes: the newest
    # matches of the whole capture, under a note saying how many older ones
    # there are (Save has them all).
    _REFILTER_MAX = 5000
    # On-screen backlog cap (matching lines); the archive always has every line.
    _PENDING_MAX = 6000
    # How often the view repaints while there is output (see _render_pending).
    _RENDER_MS = 350
    # How long the Highlight searches lines on the UI thread in one go.  Lines
    # being drawn are marked until this is spent, the rest go on screen plain,
    # and the re-marker marks lines newest first, one slice this long at a time
    # between other events.  A slow pattern then takes longer to show, but
    # never holds the window: a count of lines per slice (2,000) took 8 ms with
    # "Exception" and 8 s with "\w+.*\w+Exception".
    _HIGHLIGHT_SLICE_S = 0.025
    # A restart for a Tag/Level/App change resumes at the last line's
    # timestamp when that line is this recent (a busy log: no gap), and live
    # from now otherwise (a quiet log has nothing to miss).
    _RESUME_RECENT_S = 5.0

    def __init__(self, handler, parent=None):
        super().__init__(parent)
        self.handler = handler
        self.thread = None
        self._closed = False
        self._skipped = 0  # matching lines dropped from the on-screen backlog since the last paint
        self._paused = False
        self._paused_at = None  # capture position when Pause was pressed
        self._use_crash_buffers = False
        self._no_crash_buffer = False  # this device's logcat has none (see _crash_buffers)
        self._no_tail_option = False  # this device's logcat has no -T (see _tail_refused)
        self._filter_re = None  # the filter in effect (see _refilter_view)
        self._next_filter = (None, "")  # compiled as typed, applied when typing pauses
        self._live_from = 0  # live lines archived before this position are stale
        self._search_id = 0
        self._searching = False
        self._jobs = []
        self._hl_re = None  # keyword/regex to highlight (None = off)
        self._hl_marked = set()  # highlight patterns the on-screen lines may carry
        self._remark_job = None  # a _Remark of the view's lines under way (see _remark_step)
        self._drawn = 0  # lines ever drawn: numbers the view's lines across trims
        self._hl_fmt = None  # the highlight char-format (lazy)
        self._fmt_cache = {}  # color -> QTextCharFormat (reused per batch)
        self._carry = None  # a 'long' entry's colour, for its message lines
        self._pending = []  # lines waiting to be drawn (coalesced)
        self._spec = None  # what the running capture was started with
        self._restart = None  # the spec to restart with once the worker ends
        self._resume = None  # a live capture to resume once the device is back
        # The device came back while the capture that ended with it was still
        # reaping its adb: resume it as soon as it reports that end.
        self._resume_when_ended = False
        self._suspending = False
        self._last = None  # how the previous capture ended (for resuming)
        self._long = False  # the capture uses the 'long' format
        # A GUI-side timer paints at a FIXED low rate, decoupled from how fast
        # logcat arrives. Over RDP each repaint is a slow remote screen update,
        # so this is what stops the window going "not responding" under a flood.
        # It runs only while there is (or may be) output: a panel whose logcat
        # was never started must not tick in every open device tab forever.
        self._render_timer = QTimer(self)
        self._render_timer.timeout.connect(self._render_pending)
        # debounce the on-screen re-filter so holding a key doesn't re-render the
        # (large) buffer on every character
        self._refilter_timer = QTimer(self)
        self._refilter_timer.setSingleShot(True)
        self._refilter_timer.timeout.connect(self._refilter_view)
        self._hl_timer = QTimer(self)
        self._hl_timer.setSingleShot(True)
        self._hl_timer.timeout.connect(self._remark_view)
        self._remark_timer = QTimer(self)  # the next slice, once events are handled
        self._remark_timer.setInterval(0)
        self._remark_timer.timeout.connect(self._remark_step)
        # one restart per edit of Tag / Level / App, not one per keystroke
        self._restart_timer = QTimer(self)
        self._restart_timer.setSingleShot(True)
        self._restart_timer.timeout.connect(self._apply_restart)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        # One page toolbar: capture actions left, filters in the middle (the
        # regex box stretches), secondary actions right.  It wraps instead of
        # widening the window when the tab is narrow.
        toolbar, ctrl = page_toolbar()
        self.level = QComboBox()
        for label, name, tone in _LEVELS:
            self.level.addItem(icon(name, tone), label)
        self.level.setToolTip("Minimum log level (applied on the device; a running capture restarts)")
        self.level.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.level.setMinimumContentsLength(12)  # room for the level icon + "Verbose (all)"
        # a manual level change clears the crash preset (so it isn't stuck on
        # the crash buffers after the user moves on); guarded during the preset
        self._applying_preset = False
        self.level.currentIndexChanged.connect(self._on_level_changed)
        # Make the actual adb behaviour visible.  A follow mode uses ``-T``
        # before keeping the stream open; a dump mode uses the standard ``-d``
        # invocation and exits once the selected history is printed.
        self.hist = QComboBox()
        self.hist.addItem("Live from now", ("follow", 1))
        self.hist.addItem("Last 1,000 + live", ("follow", 1000))
        self.hist.addItem("Last 10,000 + live", ("follow", 10000))
        self.hist.addItem("Full buffer + live", ("follow", None))
        self.hist.addItem("Dump buffer once", ("dump", None))
        self.hist.addItem("Dump last 1,000 once", ("dump", 1000))
        self.hist.setToolTip(
            "Choose the exact standard logcat mode.\n\n"
            "Live modes keep following after their optional history (-T).\n"
            "Dump modes use -d, print the selected buffer once, then stop."
        )
        self.hist.currentIndexChanged.connect(self._sync_start_button)
        # compact in the toolbar; the popup still lists the full mode text
        self.hist.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.hist.setMinimumContentsLength(20)
        self.tag = QLineEdit()
        self.tag.setPlaceholderText("Tag(s)")
        self.tag.setToolTip(
            "Only these logcat tags, separated by commas or spaces. Each is "
            "matched exactly (case-sensitive) on the device, and every other "
            "tag is silenced. A running capture restarts with it when you "
            "press Enter or leave the box."
        )
        self.tag.setMaximumWidth(130)
        self.tag.editingFinished.connect(self._request_restart)
        self.app = QLineEdit()
        self.app.setPlaceholderText("App")
        self.app.setToolTip(
            "Only one app's lines: its package name (its running process is "
            "looked up with pidof) or a process ID. Needs Android 7 or later "
            "(logcat --pid). Looked up when the capture starts, so restart "
            "the capture after the app restarts."
        )
        self.app.setMaximumWidth(150)
        self.app.editingFinished.connect(self._request_restart)
        self.filt = QLineEdit()
        self.filt.setPlaceholderText("Filter (regex, any case)…")
        self._filt_tip = (
            "Show only lines matching this regular expression — any case "
            "unless Aa is on. Text that isn't a valid regex is matched as "
            "plain text. The whole capture is searched; Save still writes "
            "every line."
        )
        self.filt.setToolTip(self._filt_tip)
        self.filt.setMinimumWidth(120)
        self.filt.addAction(icon("filter"), QLineEdit.LeadingPosition)
        # a visible note when the text is matched as plain text (see compile_filter)
        self._filt_note = self.filt.addAction(icon("alert", "amber"), QLineEdit.TrailingPosition)
        self._filt_note.setVisible(False)
        self.filt.textChanged.connect(self._set_filter)
        self.btn_case = QPushButton("Aa")
        self.btn_case.setCheckable(True)
        self.btn_case.setProperty("tone", "blue")  # tinted; stronger while on
        self.btn_case.setToolTip("Match case: when on, the filter tells 'Error' from 'error'")
        self.btn_case.toggled.connect(self._on_case_toggled)
        # a crash preset: switch to the crash buffer + Error level in one click
        self.btn_crash = QPushButton("Crashes")
        self.btn_crash.setProperty("role", "ghost")
        self.btn_crash.setIcon(icon("zap", "red"))
        self.btn_crash.setToolTip(
            "Show only crashes & ANRs: the 'crash' buffer "
            "at Error level, highlighting FATAL/ANR. A running "
            "capture switches at once; otherwise click Start."
        )
        self.btn_crash.clicked.connect(self._crash_preset)
        self.hl = QLineEdit()
        self.hl.setPlaceholderText("Highlight (e.g. error|anr)…")
        self._hl_tip = (
            "Highlight matches in-line (case-insensitive regex) "
            "without hiding the rest — great for spotting "
            "errors/ANRs/your tag in a flood. The Level colours "
            "still apply; matches get a bright marker."
        )
        self.hl.setToolTip(self._hl_tip)
        self.hl.setMaximumWidth(170)
        self.hl.addAction(icon("search"), QLineEdit.LeadingPosition)
        self._hl_note = self.hl.addAction(icon("alert", "amber"), QLineEdit.TrailingPosition)
        self._hl_note.setVisible(False)
        self.hl.textChanged.connect(self._set_highlight)
        self.clear_first = QCheckBox("Clear first")
        self.btn_start = QPushButton("Start")
        self.btn_start.setProperty("role", "ok")
        self.btn_start.setIcon(icon("play", "on-accent"))
        self.btn_start.clicked.connect(self.toggle)
        self.btn_pause = QPushButton("Pause")
        self.btn_pause.setProperty("role", "ghost")
        self.btn_pause.setIcon(icon("pause", "amber"))
        self.btn_pause.clicked.connect(self._toggle_pause)
        self.btn_clear = QPushButton("Clear")
        self.btn_clear.setProperty("role", "ghost")
        self.btn_clear.setIcon(icon("eraser", "amber"))
        self.btn_clear.setToolTip("Clear the view and the captured history")
        self.btn_clear.clicked.connect(self._clear_view)
        self.btn_save = QPushButton("Save…")
        self.btn_save.setProperty("role", "ghost")
        self.btn_save.setIcon(icon("save", "teal"))
        self.btn_save.setToolTip("Save the complete logcat capture to a file")
        self.btn_save.clicked.connect(self._save)
        # primary (left)
        ctrl.addWidget(self.btn_start)
        ctrl.addWidget(self.btn_pause)
        ctrl.addSpacing(8)
        # filters (middle) — the regex filter takes the spare width
        for w in (self.level, self.hist, self.clear_first, self.tag, self.app):
            ctrl.addWidget(w)
        ctrl.addWidget(self.filt, 1)
        ctrl.addWidget(self.btn_case)
        ctrl.addWidget(self.hl)
        ctrl.addSpacing(8)
        # secondary (right)
        for w in (self.btn_crash, self.btn_clear, self.btn_save):
            ctrl.addWidget(w)
        lay.addWidget(toolbar)

        body = QVBoxLayout()
        body.setContentsMargins(12, 12, 12, 12)
        body.setSpacing(0)
        lay.addLayout(body, 1)

        self.view = _ZoomEdit()
        fam = settings_mod.get("term_font") or "Consolas"
        size = settings_mod.get("term_font_size") or settings_mod.DEFAULTS["term_font_size"]
        self.view.setFont(QFont(fam, int(size)))
        # generous on-screen scrollback; trimmed lines are archived so a long
        # capture is never lost and Save writes the COMPLETE log, not the tail
        self._sb = Scrollback(self.view, display_cap=120000)
        self._capture = _Capture(self._sb)
        # word-wrap off = far cheaper layout when lines pour in
        self.view.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.view.setObjectName("logcatView")  # terminal background (theme.py)
        self.view.setContextMenuPolicy(Qt.CustomContextMenu)
        self.view.customContextMenuRequested.connect(self._menu)
        body.addWidget(self.view, 1)
        # Empty state: a centred hint over the (terminal-coloured) view until
        # the first log line arrives; Clear brings it back.
        self._empty_hint = _EmptyLogHint(self.view.viewport())
        hint_lay = QVBoxLayout(self.view.viewport())
        hint_lay.setContentsMargins(0, 0, 0, 0)
        hint_lay.addStretch(1)
        hint_lay.addWidget(self._empty_hint, 0, Qt.AlignHCenter)
        hint_lay.addStretch(1)
        self._sync_start_button()

    def _menu(self, pos):
        m = QMenu(self.view)
        a = m.addAction(icon("copy", "blue"), "Copy")
        a.setEnabled(self.view.textCursor().hasSelection())
        a.triggered.connect(self.view.copy)
        # Only on-screen lines are selectable; "Save full logcat" has everything.
        m.addAction("Copy visible lines", lambda: (self.view.selectAll(), self.view.copy()))
        m.addAction("Select all", self.view.selectAll)
        m.addSeparator()
        m.addAction(icon("save", "teal"), "Save full logcat to file…", self._save)
        m.addAction(icon("eraser", "amber"), "Clear", self._clear_view)
        m.exec_(self.view.viewport().mapToGlobal(pos))

    _CRASH_BUFFERS = ["crash", "main", "system"]
    # Android 4 and older have no crash buffer: their logcat stops at once
    # ("Unable to open log device '/dev/log/crash'"), so the preset reads the
    # buffers crashes were logged to before it existed.
    _OLD_CRASH_BUFFERS = ["main", "system"]
    _NO_CRASH_BUFFER_RE = re.compile(r"Unable to open log device '[^']*/crash'", re.IGNORECASE)

    def _crash_buffers(self) -> list:
        return list(self._OLD_CRASH_BUFFERS if self._no_crash_buffer else self._CRASH_BUFFERS)

    def _crash_buffer_refused(self, thread, spec) -> bool:
        """Whether *thread*, a capture of the crash buffer, ended at once
        because this device's logcat has none (it said so, and no log line
        came)."""
        if not spec or "crash" not in (spec["buffers"] or ()) or thread.last_stamp:
            return False
        said = list(thread.stderr_lines) + list(thread.tail[:20])
        return any(self._NO_CRASH_BUFFER_RE.search(line) for line in said)

    @staticmethod
    def _tail_refused(thread, spec) -> bool:
        """Whether *thread*, a live capture started with -T, ended at once
        because this device's logcat has no -T (an older Android): it printed
        its usage and no log line came."""
        if (not spec or spec["mode"] != "follow" or "-T" not in thread.args
                or thread.last_stamp):
            return False
        said = list(thread.stderr_lines) + list(thread.tail[:40])
        if any(_NO_TAIL_OPTION_RE.search(line) for line in said):
            return True
        # The usage alone does not say which option was refused, and --pid
        # (Android 7+) may have been it.
        pid = thread.package or any(arg.startswith("--pid") for arg in thread.args)
        return not pid and any(_USAGE_RE.search(line) for line in said)

    def _on_level_changed(self, *_):
        if not self._applying_preset:
            self._use_crash_buffers = False
        self._request_restart()

    def _crash_preset(self):
        """One-click crash/ANR view: crash buffer, Error level, FATAL/ANR
        highlighted. Applied to the controls, and to a running capture."""
        self._applying_preset = True
        self.level.setCurrentText("Error")
        self._applying_preset = False
        self.tag.clear()
        self.hl.setText("FATAL|ANR|Exception|crash")
        self._use_crash_buffers = True
        if self._request_restart():
            self.log.emit("[INFO] crash preset set — switching the capture to crashes & ANRs")
            return
        self.log.emit(
            "[INFO] crash preset set — press Start to stream crashes "
            "& ANRs (crash buffer, Error level)"
        )

    # --- filter ---
    def _set_filter(self, text=None):
        """Compile the filter as it is typed (as plain text when it isn't a
        usable regex, with a note in the box) and schedule the redraw.  The
        new filter takes effect in :meth:`_refilter_view` once typing pauses,
        for live lines and the redraw together, so a half-typed pattern never
        flashes the view."""
        if not isinstance(text, str):
            text = self.filt.text()
        self._next_filter = compile_filter(text, self.btn_case.isChecked())
        self._show_note(self.filt, self._filt_note, self._filt_tip, self._next_filter[1])
        self._refilter_timer.start(300)  # debounced re-filter

    def _on_case_toggled(self, _checked):
        self._set_filter()

    def _refilter_view(self):
        """Apply the filter to the WHOLE capture, not just the lines on screen.

        Live lines switch to the new filter at one exact line (see _Capture),
        the archive is searched up to that line — at once while it is in
        memory, on a worker once it has spilled to disk — and the newest
        ``_REFILTER_MAX`` matches are redrawn under a note counting any older
        ones.  Save always has everything."""
        self._refilter_timer.stop()
        pattern = self._next_filter[0]
        if self._same_filter(pattern, self._filter_re):
            return  # e.g. Aa with an empty box: nothing to redraw
        self._filter_re = pattern
        keep = pattern.search if pattern is not None else None
        boundary, base = self._capture.set_keep(keep)
        self._live_from = boundary
        # queued lines were kept with the old filter; the search covers them
        self._pending = []
        self._skipped = 0
        self._search_id += 1
        search_id = self._search_id
        stop = boundary - base
        split = stop
        if self._paused and self._paused_at is not None:
            split = max(0, min(stop, self._paused_at - base))  # the rest waits for Resume
        search = (keep, stop, self._REFILTER_MAX, self._long, thread_running(self.thread))
        text = self._sb.memory_text()
        if text is not None:  # small: still in memory, nothing to wait for
            lines = text.split("\n")
            if lines and lines[-1] == "":
                lines.pop()
            self._show_search(search_id, base, split, _search_lines(lines, *search))
            return
        self._searching = True
        self._reset_view()
        self._empty_hint.set_text(_HINT_SEARCH)
        self._empty_hint.show()
        sb = self._sb

        def job():
            # Numbered from the capture's start: the lines the history limit
            # dropped keep their numbers, so the split with the live stream
            # stays at the same line.
            skipped, lines = sb.archived_lines()
            result = _search_lines(
                lines, *search, cancelled=lambda: self._search_id != search_id, first=skipped
            )
            return None if result is None else (*result, skipped)

        run_job(
            self._jobs,
            job,
            lambda result, i=search_id, b=base, s=split: self._show_search(i, b, s, result),
            lambda message, i=search_id: self._search_failed(i, message),
        )

    @staticmethod
    def _same_filter(a, b) -> bool:
        if a is None or b is None:
            return a is b
        return (a.pattern, a.flags) == (b.pattern, b.flags)

    def _show_search(self, search_id, base, split, result):
        """Redraw the view with a search's matches (a stale search is ignored).

        *result*: ``(found, total)``, and how many of the oldest lines the
        history limit dropped, when any did."""
        if self._closed or search_id != self._search_id or result is None:
            return
        self._searching = False
        found, total = result[:2]
        skipped = result[2] if len(result) > 2 else 0
        drawn = [line for index, line in found if index < split]
        held = [line for index, line in found if index >= split]
        older = total - len(found)
        if older > 0:
            what = "matching lines" if self._filter_re is not None else "lines"
            drawn.insert(0, f"… ({older} earlier {what} not redrawn — saved log has all)")
        if skipped:
            drawn.insert(0, f"… (the oldest {skipped} lines of this capture were dropped "
                            "to limit the size of its history file)")
        self._reset_view()
        self._draw_lines(drawn)
        if held:  # received while paused: shown on Resume
            self._pending = held + self._pending
            overflow = len(self._pending) - self._PENDING_MAX
            if overflow > 0:
                self._skipped += overflow
                del self._pending[:overflow]
        if drawn or self._pending:
            self._empty_hint.hide()
        elif self._capture.position()[0] > base:
            self._empty_hint.set_text(_HINT_NO_MATCH)
            self._empty_hint.show()
        else:
            self._empty_hint.set_text(_HINT_WAIT if thread_running(self.thread) else self._idle_hint())
            self._empty_hint.show()
        if self._pending or self._skipped:
            self._ensure_rendering()

    def _reset_view(self):
        """Empty the view for a redraw (which uses the current highlight)."""
        self.view.clear()
        self._carry = None
        self._hl_marked = set()
        self._remark_job = None
        self._remark_timer.stop()

    def _search_failed(self, search_id, message):
        if self._closed or search_id != self._search_id:
            return
        self._searching = False
        self._empty_hint.hide()
        self.log.emit(f"[WARNING] logcat filter: could not search the whole capture: {message}")
        self._ensure_rendering()

    def _set_highlight(self, text):
        """Keywords/regex to mark in-line (case-insensitive). Unlike the filter,
        this never hides lines — it just makes matches pop out.  New lines get
        it at once; the lines already on screen are re-marked when typing
        pauses."""
        self._hl_re, note = compile_filter(text)
        self._show_note(self.hl, self._hl_note, self._hl_tip, note)
        self._hl_timer.start(300)

    def _show_note(self, field, action, tip, note) -> None:
        """Show (or clear) a box's note: an amber mark and its explanation."""
        action.setVisible(bool(note))
        action.setToolTip(note)
        field.setToolTip(f"{note}\n\n{tip}" if note else tip)

    # --- start/stop ---
    def toggle(self):
        if thread_running(self.thread):
            self.stop()
        else:
            self.start()

    def _spec_from_controls(self, running=None) -> dict:
        """What to capture, from the toolbar.  For a restart (*running*), the
        mode and format stay those of the running capture."""
        lvl = _LEVEL_LETTER.get(self.level.currentText().split()[0], "V")
        mode, tailn = self.hist.currentData() or ("follow", 1)
        spec = {
            "fmt": settings_mod.get("logcat_format") or "threadtime",
            "buffers": self._crash_buffers() if self._use_crash_buffers else None,
            "tag": self.tag.text().strip(),
            "priority": None if lvl == "V" else lvl,
            "app": self.app.text().strip(),
            "mode": mode,
            "tail": tailn,
        }
        if running is not None:
            spec.update(fmt=running["fmt"], mode=running["mode"], tail=running["tail"])
        return spec

    @staticmethod
    def _device_side(spec) -> tuple:
        """The parts of *spec* the device applies (a change needs a restart)."""
        return (
            tuple(spec["buffers"] or ()),
            tuple(ADBHandler._logcat_tags(spec["tag"])),
            spec["priority"],
            spec["app"],
        )

    @staticmethod
    def _describe(spec) -> str:
        buffers = spec["buffers"] or ()
        parts = ["crash buffers" if "crash" in buffers else "buffers " + ", ".join(buffers)
                 ] if buffers else []
        tags = ADBHandler._logcat_tags(spec["tag"])
        parts.append(f"tag {', '.join(tags)}" if tags else "all tags")
        parts.append(f"level {spec['priority']}" if spec["priority"] else "all levels")
        if spec["app"]:
            parts.append(f"app {spec['app']}")
        return ", ".join(parts)

    def start(self):
        if self._closed or thread_running(self.thread):
            return
        spec = self._spec_from_controls()
        self._resume = None
        self._resume_when_ended = False
        self._restart = None
        clear_it = self.clear_first.isChecked() and spec["mode"] == "follow"
        args = self._launch(spec, spec["tail"], clear_first=clear_it)
        if args is None:
            return
        kind = "dump" if spec["mode"] == "dump" else "live capture"
        extra = f" (--pid of {spec['app']})" if spec["app"] and not spec["app"].isdigit() else ""
        self.log.emit(f"[OK] logcat {kind} started: adb {' '.join(args)}{extra}")

    def _launch(self, spec, tail, *, clear_first=False, seen=None):
        """Start a worker for *spec* from *tail* (see ADBHandler.logcat_args).

        On a device whose logcat has no -T (:meth:`_tail_refused`) a live
        capture runs without it, and its worker leaves out on this side the
        part of the buffer the mode does not ask for: it reads the buffer once
        and keeps its last *tail* lines, or, resuming at a time (*seen*), it
        leaves out what came before it."""
        app = spec["app"]
        pid = app if app.isdigit() else None
        here = self._no_tail_option and spec["mode"] == "follow" and tail is not None
        dump_args = None
        try:
            args = ADBHandler.logcat_args(
                buffers=spec["buffers"],
                fmt=spec["fmt"],
                tag=spec["tag"],
                priority=spec["priority"],
                dump=spec["mode"] == "dump",
                tail=None if here else tail,
                pid=pid,
            )
            if here and not (seen and seen[0]):
                dump_args = ADBHandler.logcat_args(
                    buffers=spec["buffers"], fmt=spec["fmt"], tag=spec["tag"],
                    priority=spec["priority"], dump=True, pid=pid,
                )
        except ValueError as exc:
            self.log.emit(f"[ERROR] logcat: {exc}")
            return None
        # a finished worker whose final lines were not painted yet
        self._pull(self.thread)
        self._ensure_rendering()
        self._long = spec["fmt"] == "long"
        thread = _LogcatThread(
            self.handler, args, clear_first=clear_first, capture=self._capture,
            package=app if app and not app.isdigit() else None, skip_seen=seen,
            long_entries=self._long, from_start=here,
            backlog=(tail if isinstance(tail, int) else 1) if dump_args else None,
            dump_args=dump_args,
        )
        thread.launched = (tail, seen)  # for a start again without -T
        self.thread = thread
        self._spec = spec
        thread.finished.connect(lambda t=thread: self._on_thread_finished(t))
        thread.finished.connect(thread.deleteLater)
        thread.start()
        self.btn_start.setText("Stop")
        self.btn_start.setIcon(icon("stop", "on-danger"))
        self.btn_start.setProperty("role", "danger")
        self._restyle(self.btn_start)
        if not self._empty_hint.isHidden():
            self._empty_hint.set_text(_HINT_WAIT)
        return args

    def stop(self):
        self._restart = None
        self._restart_timer.stop()
        self._resume = None
        self._resume_when_ended = False
        if self.thread is not None:
            self.thread.stop()

    def _request_restart(self, *_) -> bool:
        """A device-side filter (Tag, Level, App, crash buffers) changed: apply
        it to a running live capture.  Debounced, so one edit restarts once;
        True when a restart is on its way."""
        spec = self._spec
        if (self._closed or not thread_running(self.thread) or spec is None
                or spec["mode"] != "follow"):
            return False
        self._restart_timer.start(400)
        return True

    def _apply_restart(self):
        thread, running = self.thread, self._spec
        if (self._closed or not thread_running(thread) or running is None
                or running["mode"] != "follow" or self._restart is not None):
            return
        spec = self._spec_from_controls(running)
        if self._device_side(spec) == self._device_side(running):
            return  # nothing logcat sees has changed
        self._restart = spec
        thread.stop()  # _on_thread_finished starts the replacement

    def _resume_point(self, ended, *, recent_only):
        """``(tail, skip_seen)`` to carry on from where *ended* stopped: its
        last timestamp (lines repeated there are skipped), or the last line
        when the format has no timestamps or the log has been quiet."""
        if ended is None:
            return 1, None
        stamp, tail, at = ended
        if stamp and (not recent_only or time.monotonic() - at <= self._RESUME_RECENT_S):
            return stamp, (stamp, tail)
        return 1, (None, tail[-1:])

    def _on_thread_finished(self, thread):
        # The worker deletes itself (deleteLater) after this; drop our
        # reference first so a later Start/Stop never touches a dead wrapper.
        if self._closed or thread is not self.thread:
            return
        self._pull(thread)
        self.thread = None
        spec, self._spec = self._spec, None
        self._last = (thread.last_stamp, list(thread.tail), thread.last_at)
        restart, self._restart = self._restart, None
        suspending, self._suspending = self._suspending, False
        resume_now, self._resume_when_ended = self._resume_when_ended, False
        if restart is not None and not thread.error:
            tail, seen = self._resume_point(self._last, recent_only=True)
            self._add_status(f"filter changed ({self._describe(restart)}) — capture restarted")
            args = self._launch(restart, tail, seen=seen)
            if args is not None:
                self.log.emit(f"[OK] logcat restarted for the new filter: adb {' '.join(args)}")
                return
        if (restart is None and not suspending and not thread._stopping
                and not self._no_tail_option and self._tail_refused(thread, spec)):
            self._no_tail_option = True
            tail, seen = getattr(thread, "launched", (spec["tail"], None))
            self._add_status("this device's logcat has no -T (an older Android), so it can't "
                             "start at the newest lines: TurboADB reads its buffer once and "
                             "shows only the lines this mode asks for")
            args = self._launch(spec, tail, seen=seen)
            if args is not None:
                self.log.emit(f"[WARNING] logcat: this device's logcat has no -T — following "
                              f"without it: adb {' '.join(args)}")
                return
        if (restart is None and not suspending and not thread._stopping
                and self._crash_buffer_refused(thread, spec)):
            self._no_crash_buffer = True
            older = dict(spec, buffers=list(self._OLD_CRASH_BUFFERS))
            self._add_status("this device's logcat has no crash buffer (Android 4 and older) — "
                             "crashes are in the main and system buffers there, so those are shown")
            args = self._launch(older, older["tail"])
            if args is not None:
                self.log.emit(f"[WARNING] logcat: no crash buffer on this device — "
                              f"reading main and system instead: adb {' '.join(args)}")
                return
        self._sync_start_button()
        if suspending:
            self.log.emit("[INFO] logcat paused while the device reconnects; it resumes by itself")
        else:
            self._report_end(thread, spec)
        if resume_now and self._resume is not None:
            # the device came back while this capture was still ending
            self.resume_after_reconnect()

    def _report_end(self, thread, spec) -> None:
        """Say how the capture ended — truthfully: a failure is not a stop."""
        if thread.error:
            self.log.emit(f"[ERROR] logcat: {thread.error}")
            return
        if thread._stopping:
            self.log.emit("[OK] logcat stopped")
            return
        follow = spec is not None and spec["mode"] == "follow"
        code = thread.returncode
        detail = " | ".join(thread.stderr_lines)
        if code not in (0, None):
            text = f"logcat ended (exit {code})" + (f": {detail}" if detail else "")
        elif follow:
            text = "logcat ended by itself — the device may have disconnected" + (
                f" ({detail})" if detail else "")
        else:
            self.log.emit(f"[OK] logcat dump finished ({thread.received} lines)")
            return
        if follow and thread.received:
            # it was working: carry on once the device is back (DeviceTab)
            self._resume = {"spec": spec, "reboot": False}
            text += "; it resumes when the device is back"
        self._add_status(text)
        self.log.emit(f"[WARNING] {text}")

    # --- device loss (DeviceTab calls these around reboots and ADB restarts) ---
    def suspend_for_device_loss(self, *, reboot: bool = False) -> None:
        """The device is about to go away (a reboot, an ADB server restart):
        stop a live capture quietly and remember it for
        :meth:`resume_after_reconnect`."""
        if self._closed:
            return
        spec = self._restart or self._spec
        if thread_running(self.thread) and spec is not None and spec["mode"] == "follow":
            self._resume = {"spec": spec, "reboot": reboot}
            self._restart = None
            self._restart_timer.stop()
            self._suspending = True
            what = "rebooting" if reboot else "reconnecting"
            self._add_status(f"device {what} — capture paused")
            self.thread.stop()
        elif self._resume is not None and reboot:
            self._resume["reboot"] = True

    def resume_after_reconnect(self) -> None:
        """The device is back: restart a capture the loss interrupted — after a
        reboot from the new boot's first line, otherwise from where it
        stopped.  "Clear first" is never repeated.

        A capture whose stream has ended but whose reader is still reaping adb
        has not reported that end yet (nor asked to be resumed): it resumes as
        soon as it has (see _on_thread_finished)."""
        if self._closed:
            return
        thread = self.thread
        if thread_running(thread):
            if getattr(thread, "ending", False):
                self._resume_when_ended = True
            return
        resume, self._resume = self._resume, None
        self._resume_when_ended = False
        if resume is None:
            return
        if resume["reboot"]:
            tail, seen = None, None
            how = "device rebooted — capturing its new log from the start"
        else:
            tail, seen = self._resume_point(self._last, recent_only=False)
            how = "device back — capture resumed where it stopped"
        self._add_status(how)
        args = self._launch(resume["spec"], tail, seen=seen)
        if args is not None:
            self.log.emit(f"[OK] logcat resumed: adb {' '.join(args)}")

    def _sync_start_button(self, *_):
        """Reflect the selected adb mode while the worker is idle."""
        if thread_running(self.thread):
            return
        mode, _tailn = self.hist.currentData() or ("follow", 1)
        is_dump = mode == "dump"
        self.btn_start.setText("Dump" if is_dump else "Start")
        self.btn_start.setIcon(icon("download", "teal") if is_dump else icon("play", "on-accent"))
        self.btn_start.setProperty("role", "ghost" if is_dump else "ok")
        self._empty_hint.set_text(_HINT_DUMP if is_dump else _HINT_START)
        self.clear_first.setEnabled(not is_dump)
        self.clear_first.setToolTip(
            "Clear device logs before starting live capture."
            if not is_dump
            else "Unavailable for a dump: clearing first would erase the history you asked to save."
        )
        self._restyle(self.btn_start)

    def _toggle_pause(self):
        self._paused = not self._paused
        self._paused_at = self._capture.position()[0] if self._paused else None
        self.btn_pause.setText("Resume" if self._paused else "Pause")
        self.btn_pause.setIcon(icon("play", "green") if self._paused else icon("pause", "amber"))
        if not self._paused:
            self._ensure_rendering()  # paint what arrived while paused

    def _clear_view(self):
        self._reset_view()
        self._pending = []
        self._skipped = 0
        self._search_id += 1  # a search still running was for the old capture
        self._searching = False
        # clear forgets the archived history too; lines read before it stay out
        self._live_from = self._capture.reset()
        running = thread_running(self.thread)
        self._empty_hint.set_text(_HINT_WAIT if running else self._idle_hint())
        self._empty_hint.show()

    def _idle_hint(self) -> str:
        mode, _tailn = self.hist.currentData() or ("follow", 1)
        return _HINT_DUMP if mode == "dump" else _HINT_START

    def _on_batch(self, lines):
        """Lines that arrived without a reader thread: archive them, keep what
        the filter matches and queue it for the render timer (which paints).
        The reader thread does the same on its own side (see _pull)."""
        if not lines or self._closed:
            return
        _start, keep = self._capture.add(lines)
        if not self._empty_hint.isHidden():
            self._empty_hint.hide()  # output arrived: the view is no longer empty
        self._queue([ln for ln in lines if _kept(keep, ln)])

    def _queue(self, lines):
        """Queue kept lines for painting.  Hard cap the ON-SCREEN backlog so a
        sustained flood can never make the GUI fall behind (the archive has
        every line); the count accumulates across trims, so the marker
        reports every skip."""
        if not lines:
            return
        if not self._empty_hint.isHidden():
            self._empty_hint.hide()
        # while paused, keep buffering (bounded below) — clearing here meant
        # everything logged during a pause vanished from the view on Resume
        self._pending.extend(lines)
        overflow = len(self._pending) - self._PENDING_MAX
        if overflow > 0:
            self._skipped += overflow
            del self._pending[:overflow]

    def _add_status(self, text) -> None:
        """A line about the capture (a restart, a failure, a reconnect):
        archived with the log and shown whatever the filter says."""
        line = _MARK + text
        self._capture.add([line])
        self._queue([line])
        self._ensure_rendering()

    def _pull(self, thread):
        """Drain one worker: its kept lines and what it had to drop.  Batches
        read before the last filter change or Clear are left out — the
        redraw (or Clear) already accounts for them."""
        if thread is None:
            return
        live_from = self._live_from
        for start, lines, dropped in thread.take():
            if start < live_from:
                continue
            self._skipped += dropped
            self._queue(lines)
        if (self._filter_re is not None and not self._searching
                and not self._empty_hint.isHidden()
                and self._capture.position()[0] > live_from):
            self._empty_hint.set_text(_HINT_NO_MATCH)  # lines arrive, none match

    def _ensure_rendering(self):
        """Tick while output is running or waiting to be painted."""
        if not self._closed and not self._render_timer.isActive():
            self._render_timer.start(self._RENDER_MS)

    def _stop_rendering_if_idle(self):
        """Stop ticking once nothing is capturing and nothing is left to paint."""
        if not thread_running(self.thread) and not (self._pending or self._skipped):
            self._render_timer.stop()

    def hideEvent(self, event):
        # Another page is on screen: there is nothing to paint until this one
        # comes back.  The worker keeps capturing, archiving and filtering
        # meanwhile; what it can't hold is counted, and Save still has it.
        super().hideEvent(event)
        self._render_timer.stop()

    def showEvent(self, event):
        super().showEvent(event)
        if thread_running(self.thread) or self._pending or self._skipped:
            self._ensure_rendering()

    def _render_pending(self):
        """Pull the worker's lines, then paint what accumulated since the last
        tick — one insert, one scroll, at a fixed rate regardless of volume."""
        self._pull(self.thread)
        if self._paused or self._searching or not (self._pending or self._skipped):
            self._stop_rendering_if_idle()
            return
        lines, self._pending = self._pending, []
        if self._skipped:
            what = "matching lines" if self._filter_re is not None else "lines"
            lines.insert(0, f"… ({self._skipped} {what} skipped on screen — saved log has all)")
            self._skipped = 0
        self._draw_lines(lines)
        self._stop_rendering_if_idle()

    def _line_color(self, line, carry):
        """``(colour, carry)`` for *line*.  *carry* is the colour of the 'long'
        entry being drawn: its message lines have no level of their own."""
        if carry is not None and not _LONG_HEADER_RE.match(line):
            return (theme.LOGCAT_DEFAULT, None) if line == "" else (carry, carry)
        if _STATUS_RE.match(line):
            tone = "W" if line.startswith(_MARK) or line.startswith("- ") else "E"
            return self._LEVEL_COLOR[tone], None
        m = _LEVEL_RE.search(line, 0, 64)
        if m is None:
            return theme.LOGCAT_DEFAULT, None
        color = self._LEVEL_COLOR.get(m.group(1), theme.LOGCAT_DEFAULT)
        return color, (color if _LONG_HEADER_RE.match(line) else None)

    def _fmt_for(self, color):
        fmt = self._fmt_cache.get(color)
        if fmt is None:
            fmt = QTextCharFormat()
            fmt.setForeground(QColor(color))
            self._fmt_cache[color] = fmt
        return fmt

    def _draw_lines(self, lines):
        """Append *lines* to the view with level colouring + highlight marking.
        Shared by live rendering and the re-filter re-render.

        The highlight searches them for ``_HIGHLIGHT_SLICE_S`` at most: the
        rest go on screen plain and the re-marker marks them a slice at a time
        (see :meth:`_mark_later`), so a slow pattern never holds the window,
        not even for the redraw of a whole view after a new filter."""
        if not lines:
            return
        sb = self.view.verticalScrollBar()
        at_bottom = sb.value() >= sb.maximum() - 4
        cur = self.view.textCursor()
        cur.movePosition(QTextCursor.End)
        hre = self._hl_re
        hl_fmt = self._highlight_fmt() if hre is not None else None
        budget = self._HIGHLIGHT_SLICE_S  # left for searching these lines
        unmarked = None  # the number of the first line drawn without its marks
        clock = time.perf_counter
        carry = self._carry
        for index, line in enumerate(lines):
            color, carry = self._line_color(line, carry)
            fmt = self._fmt_for(color)
            hit = False
            if hre is not None and unmarked is None:
                if budget < 0:
                    unmarked = self._drawn + index
                else:
                    started = clock()
                    hit = hre.search(line) is not None
                    budget -= clock() - started
            # Fast path (the common case): no highlight, or this line has no match
            # — a single insertText keeps the flood cheap. Only split a line into
            # segments when it actually contains a match.
            if hit:
                self._insert_highlighted(cur, line, fmt, hre, hl_fmt)
            else:
                cur.insertText(line + "\n", fmt)
        self._carry = carry
        self._drawn += len(lines)
        if hre is not None:
            self._hl_marked.add(hre)
            if unmarked is not None:
                self._mark_later(unmarked, self._drawn - 1)
        if at_bottom:
            sb.setValue(sb.maximum())

    def _mark_later(self, first, last):
        """Lines *first* to *last* (numbered as drawn) went on screen without
        the highlight's marks: the re-marker gives them theirs, newest first."""
        job = self._remark_job
        if job is None:
            job = self._remark_job = _Remark(self._hl_re, [], [], whole=False)
        elif job.pattern is not self._hl_re:
            return  # a new highlight is being typed: its re-mark of the view has them
        if job.spans and job.spans[0][0] == first - 1:
            job.spans[0][0] = last  # they continue the newest span still to do
        else:
            job.spans.insert(0, [last, first])
        if not self._remark_timer.isActive():
            self._remark_timer.start()

    def _remark_view(self):
        """Re-apply the highlight to the lines already on screen (new lines get
        it as they are drawn).  Newest first and a slice at a time between
        other events, so even the 120,000-line view never freezes the window;
        only lines the old or the new pattern matches are touched."""
        new = self._hl_re
        olds = [p for p in self._hl_marked if p is not new]
        if new is not None:
            self._hl_marked.add(new)
        if new is None and not olds:
            self._remark_job = None
            return
        self._remark_job = _Remark(new, olds, [[self._drawn - 1, 0]], whole=True)
        self._remark_step()

    def _remark_step(self):
        """One slice of the re-marker: lines newest first until
        ``_HIGHLIGHT_SLICE_S`` is spent (one line at least), then the rest
        after the events that came in meanwhile."""
        job = self._remark_job
        if job is None or self._closed:
            self._finish_remark(job)
            return
        doc = self.view.document()
        top = self._drawn - (doc.blockCount() - 1)  # number of the view's top line
        new, olds = job.pattern, job.olds
        hl_fmt = self._highlight_fmt() if new is not None else None
        deadline = time.perf_counter() + self._HIGHLIGHT_SLICE_S
        cur = QTextCursor(doc)
        cur.beginEditBlock()
        while job.spans:
            span = job.spans[0]
            stop = max(span[1], top)  # lines trimmed off the top need nothing
            block = doc.findBlockByNumber(span[0] - top) if span[0] >= stop else None
            while block is not None and block.isValid() and span[0] >= stop:
                self._remark_block(cur, block, new, olds, hl_fmt)
                block = block.previous()
                span[0] -= 1
                if time.perf_counter() >= deadline:
                    break
            if span[0] < stop or block is None or not block.isValid():
                job.spans.pop(0)
            if time.perf_counter() >= deadline:
                break
        cur.endEditBlock()
        if not job.spans:
            self._finish_remark(job)
        elif not self._remark_timer.isActive():
            self._remark_timer.start()

    def _remark_block(self, cur, block, new, olds, hl_fmt) -> None:
        """Mark *block* for *new* (None: no highlight), taking off the marks
        of *olds*; a line neither matches is left alone."""
        text = block.text()
        hit = new is not None and new.search(text) is not None
        if hit or any(p.search(text) for p in olds):
            pos = block.position()
            base = self._base_format(cur, block, text)
            cur.setPosition(pos)
            cur.setPosition(pos + block.length() - 1, QTextCursor.KeepAnchor)
            cur.setCharFormat(base)
            for m in new.finditer(text) if hit else ():
                if m.end() > m.start():
                    cur.setPosition(pos + self._u16(text, m.start()))
                    cur.setPosition(pos + self._u16(text, m.end()), QTextCursor.KeepAnchor)
                    cur.setCharFormat(hl_fmt)

    def _finish_remark(self, job):
        self._remark_timer.stop()
        if job is not None and job is self._remark_job:
            self._remark_job = None
            if job.whole:  # every line has been through it: only its marks remain
                self._hl_marked = {job.pattern} if job.pattern is not None else set()

    def _base_format(self, cur, block, text):
        """The level colour *block* was drawn in (read back from the view, so
        a 'long' entry's message lines keep their entry's colour)."""
        if text:
            cur.setPosition(block.position() + 1)
            fmt = cur.charFormat()
            if fmt.background().color() != QColor(theme.HIGHLIGHT_BG):
                return self._fmt_for(fmt.foreground().color().name())
        return self._fmt_for(self._line_color(text, None)[0])

    @staticmethod
    def _u16(text, index) -> int:
        """*index* of *text* in Qt's UTF-16 units (an emoji takes two)."""
        head = text[:index]
        return index if head.isascii() else len(head.encode("utf-16-le")) // 2

    def _highlight_fmt(self):
        """The in-line marker format (bright amber, bold) — built once."""
        if self._hl_fmt is None:
            f = QTextCharFormat()
            f.setBackground(QColor(theme.HIGHLIGHT_BG))
            f.setForeground(QColor(theme.HIGHLIGHT_FG))
            f.setFontWeight(QFont.Bold)
            self._hl_fmt = f
        return self._hl_fmt

    @staticmethod
    def _insert_highlighted(cur, line, base_fmt, hre, hl_fmt):
        """Insert *line* with matched spans in *hl_fmt* and the rest in *base_fmt*."""
        pos = 0
        for m in hre.finditer(line):
            s, e = m.start(), m.end()
            if e == s:  # skip zero-width matches
                continue
            if s > pos:
                cur.insertText(line[pos:s], base_fmt)
            cur.insertText(line[s:e], hl_fmt)
            pos = e
        cur.insertText(line[pos:] + "\n", base_fmt)

    def _save(self):
        """Save the COMPLETE log (not just the tail) off the UI thread."""
        from .fileutil import save_output

        save_output(
            self,
            "Save logcat",
            "turboadb-logcat-" + time.strftime("%Y%m%d-%H%M%S") + ".log",
            self._sb.save_job(),
            what="logcat",
            on_saved=lambda path: self.log.emit(f"[OK] logcat saved to {path}"),
        )

    @staticmethod
    def _restyle(w):
        w.style().unpolish(w)
        w.style().polish(w)

    def close_panel(self):
        """Stop capture and detach the worker without waiting on the UI thread."""
        self._closed = True
        for timer in (self._render_timer, self._refilter_timer, self._hl_timer,
                      self._remark_timer, self._restart_timer):
            timer.stop()
        self._remark_job = None
        self._search_id += 1  # a running search ends at its next check
        close_jobs(self._jobs)
        self._resume = self._restart = None
        thread, self.thread = self.thread, None
        if thread is not None:
            thread.stop()
            disconnect_signals(thread, ("finished",))
            park_thread(thread)  # re-adds deleteLater; referenced until it ends
            thread.take()  # its unread backlog (up to _LINES_MAX lines)
        self._sb.close()  # drop the temp scrollback file
        # The paint backlog and the on-screen document are the panel's big
        # buffers: release them now, not whenever the closed tab's widgets
        # are finally deleted.
        self._pending = []
        self._skipped = 0
        self._fmt_cache.clear()
        self.view.clear()
