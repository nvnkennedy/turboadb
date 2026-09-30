"""What the terminal colours by itself (pure Python, no Qt).

A device shell on a terminal prints a plain prompt and ``adb logcat`` plain
lines; the console colours the prompt's parts, logcat lines by priority, and
in other output only the words that say what happened.  Only lines that
clearly are logcat lines count, in any of logcat's formats, so ordinary
output that merely resembles one keeps its own look; only words of their
own count, so a file named ``error.log`` or ``granted=true`` stays plain."""

import pytest

from turboadb.gui import shell_colors
from turboadb.gui.shell_colors import (
    LogcatLines, is_long_header, logcat_level, may_mean, meaning_spans, prompt_spans,
)


# Lines in each of logcat's layouts (as logprint.cpp formats them).
@pytest.mark.parametrize("line, level", [
    # threadtime, the default
    ("09-29 16:42:56.210  5927  5927 I Finsky  : [2] amrb - Received: PACKAGE_REMOVED", "I"),
    ("09-29 16:42:56.210     1     1 E init    : failed", "E"),
    ("09-29 16:42:56.210 12345 12346 W ActivityManager: slow", "W"),
    ("09-29 16:42:56.210  2011  2400 D NetworkMonitor/100: probe", "D"),
    ("09-29 16:42:56.210  2011  2400 V Tag     : ", "V"),
    ("09-29 16:42:56.210  2011  2400 F libc    : Fatal signal 11", "F"),
    ("09-29 16:42:56.210  2011  2400 A DEBUG   : assert", "A"),
    # -v year, usec, nsec, zone, uid, epoch, monotonic
    ("2026-09-29 16:42:56.210  5927  5927 I Finsky  : x", "I"),
    ("09-29 16:42:56.210123  5927  5927 W Finsky  : x", "W"),
    ("09-29 16:42:56.210123456  5927  5927 E Finsky  : x", "E"),
    ("09-29 16:42:56.210 +0530  5927  5927 I Finsky  : x", "I"),
    ("09-29 16:42:56.210  root:  123   456 I init    : x", "I"),
    ("09-29 16:42:56.210 10123: 5927  5927 D Finsky  : x", "D"),
    ("1727600576.210  5927  5927 I Finsky  : x", "I"),
    ("   12345.678  5927  5927 E Finsky  : x", "E"),
    # time
    ("09-29 16:42:56.210 I/Finsky  ( 5927): [2] amrb", "I"),
    ("09-29 16:42:56.210 E/AndroidRuntime(12345): FATAL EXCEPTION", "E"),
    ("09-29 16:42:56.210 W/Finsky  ( root: 5927): x", "W"),
    # brief
    ("I/Finsky  ( 5927): [2] amrb", "I"),
    ("E/AndroidRuntime(12345): FATAL EXCEPTION: main", "E"),
    ("D/Tag with space( 5927): x", "D"),
    # process and thread
    ("I( 5927) [2] amrb  (Finsky)", "I"),
    ("W( 5927: 5930) x", "W"),
    # tag
    ("I/Finsky  : [2] amrb", "I"),
    ("W/ActivityManager: Slow operation", "W"),
    # long's header
    ("[ 09-29 16:42:56.210  5927: 5927 I/Finsky   ]", "I"),
    ("[ 09-29 16:42:56.210  5927: 5927 E/AndroidRuntime ]", "E"),
    ("[ 1727600576.210  5927: 5927 D/Tag      ]", "D"),
])
def test_logcat_lines_in_every_format(line, level):
    assert logcat_level(line) == level


@pytest.mark.parametrize("line", [
    "",
    "hello world",
    "drwxrwx--x  4 system system 4096 2026-09-29 16:42 data",
    "-rw-r--r--  1 root root 12 09-29 16:42 file",
    "[   12.345678] init: starting service 'adbd'...",
    "[    0.000000] Booting Linux on physical CPU 0x0",
    "I/O error: disk full",
    "I/O error (5): bad block",
    "E/x: short tag",
    "V2318:/ $ ls",
    "130|V2318:/tmp $ ",
    "  PID USER         PR  NI VIRT  RES  SHR S[%CPU] %MEM     TIME+ ARGS",
    " 5927 u0_a123      20   0  10G 150M  90M S  1.0   2.0   0:03.00 com.android.vending",
    "Filesystem            Size  Used Avail Use% Mounted on",
    "Traceback (most recent call last):",
    "  File \"x.py\", line 1, in <module>",
    "09-29 16:42:56.210 something else entirely",
    "09-29 16:42:56.210  5927  5927 X Finsky  : not a priority letter",
    "--------- beginning of main",
    "64 bytes from 8.8.8.8: icmp_seq=1 ttl=117 time=20.1 ms",
])
def test_lines_that_are_not_logcat_lines(line):
    assert logcat_level(line) is None


def test_long_format_message_lines_share_their_headers_priority():
    lines = LogcatLines()
    assert is_long_header("[ 09-29 16:42:56.210  5927: 5927 E/AndroidRuntime ]")
    assert not is_long_header("09-29 16:42:56.210  5927  5927 E AndroidRuntime: x")
    assert lines.level("[ 09-29 16:42:56.210  5927: 5927 E/AndroidRuntime ]") == "E"
    assert lines.level("FATAL EXCEPTION: main") == "E"
    assert lines.level("\tat com.example.Main.run(Main.java:10)") == "E"
    assert lines.level("") is None  # the empty line that ends the entry
    assert lines.level("plain text again") is None
    assert lines.level("[ 09-29 16:42:57.000  5927: 5927 I/Finsky   ]") == "I"
    assert lines.level("09-29 16:42:57.001  5927  5927 W Finsky  : threadtime") == "W"
    assert lines.level("not carried after another format") is None
    lines.level("[ 09-29 16:42:57.000  5927: 5927 I/Finsky   ]")
    lines.reset()
    assert lines.level("message") is None


def parts(line):
    spans = prompt_spans(line)
    return None if spans is None else [(line[a:b], name) for a, b, name in spans]


@pytest.mark.parametrize("line, expected", [
    ("V2318:/ $ ", [("V2318", "host"), (":", "colon"), ("/", "path"), ("$", "mark")]),
    ("130|V2318:/tmp $ ", [("130|", "status"), ("V2318", "host"), (":", "colon"),
                           ("/tmp", "path"), ("$", "mark")]),
    ("V2318:/data/local/tmp # ", [("V2318", "host"), (":", "colon"),
                                  ("/data/local/tmp", "path"), ("#", "root")]),
    ("shell@android:/sdcard $ ", [("shell@", "user"), ("android", "host"), (":", "colon"),
                                  ("/sdcard", "path"), ("$", "mark")]),
    ("PD2318:/sdcard/My Folder $ ", [("PD2318", "host"), (":", "colon"),
                                     ("/sdcard/My Folder", "path"), ("$", "mark")]),
    ("/ # ", [("/", "path"), ("#", "root")]),
    ("$ ", [("$", "mark")]),
    ("1|# ", [("1|", "status"), ("#", "root")]),
    ("V2318:/ $", [("V2318", "host"), (":", "colon"), ("/", "path"), ("$", "mark")]),
])
def test_prompt_parts(line, expected):
    assert parts(line) == expected


def test_the_command_after_a_prompt_is_not_part_of_it():
    line = "V2318:/sdcard $ echo $ HOME # not a prompt"
    spans = prompt_spans(line)
    assert spans[-1] == (14, 15, "mark")
    assert line[spans[-1][1]:] == " echo $ HOME # not a prompt"


@pytest.mark.parametrize("line", [
    "hello $ world",
    "total 24",
    "ls: /x: No such file or directory",
    "  $ indented",
    "V2318:/ >",
])
def test_lines_that_do_not_start_with_a_prompt(line):
    assert prompt_spans(line) is None


# --------------------------------------------------------------------------- #
# the words that say what happened
# --------------------------------------------------------------------------- #
def words(line):
    return [(line[a:b], kind) for a, b, kind in meaning_spans(line)]


@pytest.mark.parametrize("line, expected", [
    ("ls: /data/system: Permission denied", [("Permission denied", "error")]),
    ("rm: /system/app: Read-only file system", [("Read-only file system", "error")]),
    ("cat: /sdcard/x.txt: No such file or directory", [("No such file or directory", "error")]),
    ("/system/bin/sh: foo: inaccessible or not found", [("not found", "error")]),
    ("Failure [INSTALL_FAILED_VERSION_DOWNGRADE: Downgrade detected]",
     [("Failure", "error"), ("INSTALL_FAILED_VERSION_DOWNGRADE", "error")]),
    ("failed to connect to '10.0.0.2:5555': Connection refused",
     [("failed", "error"), ("Connection refused", "error")]),
    ("error: device unauthorized.", [("error", "error"), ("unauthorized", "error")]),
    ("emulator-5554\toffline", [("offline", "error")]),
    ("java.lang.SecurityException: Permission Denial: not allowed to send broadcast",
     [("java.lang.SecurityException", "error"), ("Permission Denial", "error"),
      ("not allowed", "error")]),
    ("Caused by: java.io.IOException: Broken pipe", [("java.io.IOException", "error")]),
    ("Get-Item : Cannot find path 'C:\\foo' because it does not exist.",
     [("Cannot", "error"), ("does not exist", "error")]),
    ("'foo' is not recognized as an internal or external command,", [("not recognized", "error")]),
    ("Access is denied.", [("Access is denied", "error")]),
    ("the request was blocked by policy", [("blocked", "error")]),
    ("Segmentation fault", [("Segmentation fault", "error")]),
    ("WARNING: linker: libfoo.so: unused DT entry", [("WARNING", "warning")]),
    ("Warning: deprecated option -r", [("Warning", "warning"), ("deprecated", "warning")]),
    ("disconnected 10.0.0.2:5555", [("disconnected", "warning")]),
    ("Success", [("Success", "success")]),
    ("connected to 10.0.0.2:5555", [("connected to", "success")]),
    ("already connected to 10.0.0.2:5555", [("already connected to", "success")]),
    ("* daemon started successfully", [("successfully", "success")]),
    ("/sdcard/a.mp4: 1 file pushed, 0 skipped. 38.1 MB/s", [("pushed", "success")]),
    ("        1 file(s) copied.", [("copied", "success")]),
    ("OK:/bugreports/bugreport.zip", [("OK", "success")]),
    ("5 passed, 0 failed, 0 errors",
     [("passed", "success"), ("0 failed", "success"), ("0 errors", "success")]),
    ("12 passed, 10 failed", [("passed", "success"), ("failed", "error")]),
])
def test_words_that_say_what_happened(line, expected):
    assert words(line) == expected


@pytest.mark.parametrize("line", [
    "drwxr-xr-x  2 root root 4096 2026-09-30 10:00 error_logs",
    "-rw-r--r--  1 root root  220 2026-09-30 10:00 error.log",
    "-rw-r--r--  1 root root  220 2026-09-30 10:00 failed-uploads.txt",
    "/data/local/tmp/fail",
    "com.android.fail.provider",
    "    android.permission.CAMERA: granted=false, flags=[ USER_SENSITIVE_WHEN_DENIED ]",
    "  mLastError=0 mFailedCount=3 onError()",
    "  lastError=null errors=0 installed=true",
    "non-fatal: continuing",
    "--------- beginning of crash",
    "0123456789ABCDEF\tdevice",
    "LOOKUP_TOKEN BOOK",
    "total 24",
    "",
])
def test_words_that_are_part_of_something_else(line):
    assert meaning_spans(line) == []


@pytest.mark.parametrize("kind, phrases", [
    ("error", shell_colors._ERROR_PHRASES),
    ("warning", shell_colors._WARNING_PHRASES),
    ("success", shell_colors._SUCCESS_PHRASES),
])
def test_every_phrase_is_found_in_any_case(kind, phrases):
    """The quick look (may_mean) never rules out a line one of them is in."""
    for phrase in phrases:
        for text in (phrase, phrase.upper(), phrase.capitalize()):
            assert may_mean(f"x: {text}."), text
            assert words(f"x: {text}.") == [(text, kind)], text


def test_a_very_long_line_is_not_searched():
    assert may_mean("x " * 1000 + "error")
    assert not may_mean("x" * 5000 + " error")
    assert meaning_spans("x" * 5000 + " error") == []
