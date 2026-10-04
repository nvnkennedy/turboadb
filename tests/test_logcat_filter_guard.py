"""The Logcat filter never runs a pattern that backtracks for minutes.

Python's matcher holds the GIL, so a catastrophic pattern froze the whole
window whichever thread ran it.  Nested repeats were already matched as plain
text; so is now a repeat whose body can match the same text in two ways, such
as ``(a|aa)+``, while ordinary alternations inside a repeat stay patterns.
Repeats in a row (``\\w+.*\\w+Exception``) passed both checks and took
milliseconds per line: a pattern that is too slow on a long line is timed out
too.  So is one whose repeats can share a run of one character
(``\\s*\\s*x``): only a long run of spaces or dashes slows it down, for a minute
or more.  The run may be of any character the pattern names (``/+\\s*/+x``)."""

import re
import time

import pytest

pytest.importorskip("PyQt5")

from turboadb.gui.logcat_view import compile_filter  # noqa: E402

AMBIGUOUS = [
    r"(a|aa)+$",        # one alternative repeats another
    r"(a|a)+$",         # two the same
    r"(a|b|ab)*$",      # one is two others in a row
    r"(a?a)+$",         # an optional part, then the same character
    r"(\d?\d)+$",
    r"(.|\s)+$",        # a class that takes the other's characters
    r"(a|)+$",          # one that matches nothing
    r"(a|aa){1,50}$",   # a bounded repeat is enough
    r"(?i)(ERROR|error)+x",  # the same text once case is ignored
]

ORDINARY = [
    r"error|warn",
    r"(error|warn)+",
    r"(foo|bar|baz)*end",  # bar and baz share a start but part later
    r"(on|off|one)+x",
    r"(\w|\d)+",           # the parser makes this one character class
    r"(ab|ac)+",
    r"(https?://)+",
    r"E/.*Tag",
    r"(Activity|Window)Manager.*(start|stop)",
    r"^\d{2}-\d{2} \d{2}:\d{2}",
    r"pid=\d+",
    r"FATAL|ANR|Exception|crash",
    # these three are timed too (see SLOW) and are nowhere near too slow
    r"Exception",
    r".*Exception.*",
    r"ActivityManager|WindowManager",
    # one repeat, or several that share no character, goes over a run of one
    # character once from each start: nothing to refuse
    r"\s+$",
    r"\d+\s*(ms|s|us|ns)",
    r"\s+(error|warn|fail|fatal|crash|exception)",
    r"-+\s*(beginning|end) of",
    r"^\s*\w*\s*:",  # from the start of the line only
    # ... and so over a run of a character they name themselves
    r"//+\s*TODO",
    r"x+y+z",
    r"e.*x",
    r"/data/\S+/files",
    r"\t+\w+",
]

# Repeats in a row: no nesting and no overlapping choices, but on a 180
# character line "\w+.*\w+Exception" took 3.7 ms as a Filter, and 7.8 s for
# one slice of the Highlight's re-mark (8 ms with "Exception").
SLOW = [
    r"\w+.*\w+Exception",
    r".* .*timeout",
    r".*.*x",
]

# Repeats that can share a run of one character.  They search the lines the
# others are timed on in no time, as nothing there repeats for long.  One log
# line with 3,500 spaces took 97 s with the first, one with 3,500 dashes 78 s
# with the second.  ``(pattern, the character of the run)``.
SHARING_A_RUN = [
    (r"\s*\s*x", " "),
    (r"-+\s*-+x", "-"),
    (r"=+ ?=+x", "="),
    (r"\d+\d+x", "0"),
    (r"\.+\.*x", "."),
    (r"[a-z]+[a-z]*!", "a"),
    (r"\s*\w*\s*:", " "),
    (r"\s*\s*$", " "),  # slow when the line goes on after the run
    # a run of any character the pattern names: a log line can hold one of
    # slashes, tildes or one letter as well as one of dashes
    (r"/+\s*/+x", "/"),
    (r"~+~+x", "~"),
    (r"x*x*y", "x"),
    (r"1+1+x", "1"),
    (r"[/~]+[/~]*!", "~"),
    (r"\t*\t*x", "\t"),      # named by an escape
    (r"\x2f+\x2f+!", "/"),   # ... or by its code
    (r"[:-<]+[:-<]*x", "<"),  # the end of a range
]


@pytest.mark.parametrize("text", AMBIGUOUS)
def test_a_repeat_with_overlapping_choices_is_matched_as_plain_text(text):
    pattern, note = compile_filter(text)
    assert pattern.pattern == re.escape(text)
    assert "(a|aa)+" in note and "plain text" in note
    started = time.perf_counter()
    pattern.search("a" * 5000 + "1" * 5000 + " " * 5000 + "!")
    assert time.perf_counter() - started < 0.5


@pytest.mark.parametrize("text", ORDINARY)
def test_ordinary_patterns_stay_patterns(text):
    pattern, note = compile_filter(text)
    assert note == ""
    assert pattern.pattern == text and pattern.flags & re.IGNORECASE


@pytest.mark.parametrize("text", SLOW)
def test_a_pattern_too_slow_for_a_long_line_is_matched_as_plain_text(text):
    started = time.perf_counter()
    pattern, note = compile_filter(text)
    assert time.perf_counter() - started < 2.0  # timing it stops at the first slow line
    assert pattern.pattern == re.escape(text)
    assert "too long" in note and "plain text" in note
    line = "09-27 12:00:00.123  1234  5678 W ActivityManager: " + "done updating stats " * 7
    started = time.perf_counter()
    for _ in range(200):
        pattern.search(line)
    assert time.perf_counter() - started < 0.5


@pytest.mark.parametrize("text, char", SHARING_A_RUN)
def test_repeats_that_share_a_run_of_one_character_are_matched_as_plain_text(text, char):
    started = time.perf_counter()
    pattern, note = compile_filter(text)
    assert time.perf_counter() - started < 2.0  # timing it stops at the first slow run
    assert pattern.pattern == re.escape(text)
    assert "too long" in note and "plain text" in note
    started = time.perf_counter()
    pattern.search(char * 3500 + " end of the line")
    assert time.perf_counter() - started < 0.5


def test_a_run_is_checked_with_match_case_on_too():
    assert compile_filter(r"[A-Z]+[A-Z]*!", match_case=True)[1]
    assert compile_filter(r"\s*\s*x", match_case=True)[1]
    assert compile_filter(r"[A-Z]+\d+", match_case=True)[1] == ""
    assert compile_filter(r"X+X*!", match_case=True)[1]
    assert compile_filter(r"X+Y+!", match_case=True)[1] == ""


def test_the_check_follows_match_case():
    assert compile_filter(r"(ERROR|error)+x")[1]  # any case: the same text twice
    assert compile_filter(r"(ERROR|error)+x", match_case=True)[1] == ""


def test_a_long_plain_filter_is_checked_at_once():
    started = time.perf_counter()
    for text in ("x" * 2000, r"\d+ " * 300, "|".join(f"word{i}" for i in range(300))):
        assert compile_filter(text)[1] == ""
    assert time.perf_counter() - started < 1.0


def test_the_panel_says_why_it_matches_plain_text(qapp):
    from turboadb.gui.logcat_view import LogcatPanel

    p = LogcatPanel(None)
    try:
        lines = ["a" * 40 + "!", "(a|aa)+ literally"]
        p._on_batch(lines)
        p.filt.setText("(a|aa)+")
        p._refilter_view()
        assert p.view.toPlainText().splitlines() == [lines[1]]
        assert p._filt_note.isVisible() and "(a|aa)+" in p.filt.toolTip()
    finally:
        p.close_panel()
        p.deleteLater()
