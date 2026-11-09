"""Tab in the Android shell reads the line as the shell does.

The line used to be split on spaces: a name with a space in it broke the
command, the word after ``;`` or ``|`` was looked up as a file, Tab after a
space did nothing, and built-ins such as ``cd`` were never offered.

No device is used: the "device" prints what its ``ls`` would.
"""
import pytest

pytest.importorskip("PyQt5")

from turboadb.results import CommandResult, OperationResult  # noqa: E402


@pytest.mark.parametrize("line, start, text, command, quote", [
    ("da", 0, "da", True, None),
    ("/da", 0, "/da", False, None),
    ("ls da", 3, "da", False, None),
    ("ls /da", 3, "/da", False, None),
    ("ls; da", 4, "da", True, None),
    ("ls;da", 3, "da", True, None),
    ("ls | gr", 5, "gr", True, None),
    ("ls|gr", 3, "gr", True, None),
    ("true && da", 8, "da", True, None),
    ("false || da", 9, "da", True, None),
    ("sleep 1 & da", 10, "da", True, None),
    ("echo $(da", 7, "da", True, None),
    ("ls > da", 5, "da", False, None),
    ("ls >da", 4, "da", False, None),
    ("cat < da", 6, "da", False, None),
    ("ls 2>/da", 5, "/da", False, None),
    ("ls ", 3, "", False, None),
    ("ls; ", 4, "", True, None),
    ("ls >", 4, "", False, None),
    ("FOO=1 da", 6, "da", True, None),
    ("> out da", 6, "da", True, None),
    ("ls; /da", 4, "/da", False, None),
    (r"cd /sdcard/My\ Fo", 3, "/sdcard/My Fo", False, None),
    ('cd "/sdcard/My Fo', 3, "/sdcard/My Fo", False, '"'),
    ("cd '/sdcard/My Fo", 3, "/sdcard/My Fo", False, "'"),
    ('cd "/sdcard/My Folder"/su', 3, "/sdcard/My Folder/su", False, None),
    ("echo 'a;b' da", 11, "da", False, None),
    (r"echo a\;b da", 10, "da", False, None),
    (r'cd "a\"b', 3, 'a"b', False, '"'),
])
def test_the_word_being_typed_is_read_as_the_shell_reads_it(line, start, text, command, quote):
    from turboadb.gui.device_tab import _last_shell_word

    assert tuple(_last_shell_word(line)) == (start, text, command, quote)


@pytest.mark.parametrize("text, quote, closed, written", [
    ("/sdcard/My Folder/", None, False, r"/sdcard/My\ Folder/"),
    ("it's (1) & $x", None, False, r"it\'s\ \(1\)\ \&\ \$x"),
    ("/sdcard/My Folder/", '"', False, '"/sdcard/My Folder/'),
    ("My File", '"', True, '"My File"'),
    ('a"b$c', '"', True, '"a\\"b\\$c"'),
    ("it's", "'", True, "'it'\\''s'"),
    ("plain-name_1.txt", None, False, "plain-name_1.txt"),
])
def test_a_name_goes_on_the_line_as_one_word(text, quote, closed, written):
    from turboadb.gui.device_tab import _word_for_line

    assert _word_for_line(text, quote, closed=closed) == written


class _Device:
    """A device whose ``ls`` prints *listing*."""

    serial = "PHONE1"
    config = None

    def __init__(self, listing=""):
        self.listing = listing
        self.commands = []

    def shell(self, command, timeout=None, safe=None, **_kw):
        self.commands.append(command)
        return OperationResult(True, "shell", value=CommandResult(command, 0, self.listing, "", 0.0))


@pytest.fixture
def complete(qapp):
    """``complete(line, listing)``: the device's answer to Tab on *line*."""
    from turboadb.gui.device_tab import _TerminalWidgetBase

    widgets = []

    def run(line, listing="", cwd="/"):
        device = _Device(listing)
        widget = _TerminalWidgetBase(device)
        widgets.append(widget)
        run.device = device
        return widget._query_android_completion(line, cwd)

    yield run
    for widget in widgets:
        widget.deleteLater()


# ---- names with spaces -------------------------------------------------------
def test_a_name_with_a_space_is_escaped_on_the_line(complete):
    assert complete("ls /sdcard/My", "/sdcard/My Folder/\n") == (r"ls /sdcard/My\ Folder/", [])


def test_a_typed_escape_is_searched_for_as_the_name_it_means(complete):
    answer = complete(r"cat /sdcard/My\ Fo", "/sdcard/My Folder/\n")
    assert answer == (r"cat /sdcard/My\ Folder/", [])
    assert "ls -dp '/sdcard/My Fo'*" in complete.device.commands[0]


def test_an_open_quote_stays_open_for_a_folder(complete):
    assert complete('cd "/sdcard/My', "/sdcard/My Folder/\n") == ('cd "/sdcard/My Folder/', [])


def test_an_open_quote_closes_on_a_file(complete):
    assert complete("cat '/sdcard/My", "/sdcard/My File.txt\n") == ("cat '/sdcard/My File.txt'", [])


def test_a_quote_in_a_name_cannot_break_out_of_the_listing_command(complete):
    complete(r"cat it\'s", "")
    assert "ls -dp 'it'\\''s'*" in complete.device.commands[0]


def test_an_unescaped_apostrophe_opens_a_quote_as_it_does_in_a_shell(complete):
    complete("cat it's", "")
    assert "ls -dp 'its'*" in complete.device.commands[0]


def test_several_names_with_spaces(complete):
    line, options = complete("ls /sdcard/My", "/sdcard/My Files/\n/sdcard/My Folder/\n")
    assert line == r"ls /sdcard/My\ F"
    assert options == [r"/sdcard/My\ Files/", r"/sdcard/My\ Folder/"]
    assert [o.display for o in options] == ["My Files/", "My Folder/"]
    assert {o.start for o in options} == {3}


# ---- commands and files in their places --------------------------------------
@pytest.mark.parametrize("line, completed", [
    ("ls; logc", "ls; logcat "),
    ("ls;logc", "ls;logcat "),
    ("cat x | logc", "cat x | logcat "),
    ("true && logc", "true && logcat "),
])
def test_after_a_separator_the_word_is_a_command(complete, line, completed):
    assert complete(line, "/system/bin/logcat\n") == (completed, [])
    assert complete.device.commands[0].startswith("ls -d /system/bin/")


def test_after_a_redirect_the_word_is_a_file(complete):
    assert complete("ls > /sd", "/sdcard/\n") == ("ls > /sdcard/", [])
    assert "ls -dp '/sd'*" in complete.device.commands[0]


def test_a_path_as_first_word_is_a_path(complete):
    line, options = complete("/da", "/data/\n/data_mirror/\n")
    assert (line, options) == ("/data", ["/data/", "/data_mirror/"])
    assert "/system/bin" not in complete.device.commands[0]


# ---- Tab after a space -------------------------------------------------------
def test_tab_after_a_space_lists_the_folder(complete):
    line, options = complete("ls ", "Download/\nnotes.txt\n", cwd="/sdcard")
    assert line is None and options == ["Download/", "notes.txt"]
    assert complete.device.commands[0] == "cd '/sdcard' 2>/dev/null; ls -dp ''* 2>/dev/null"
    assert {o.start for o in options} == {3}


def test_tab_after_a_space_with_one_entry_completes_it(complete):
    assert complete("cat ", "only.txt\n") == ("cat only.txt", [])


def test_an_empty_line_still_asks_the_device_nothing(complete):
    assert complete("   ", "x\n") == (None, [])
    assert complete.device.commands == []


# ---- built-ins ---------------------------------------------------------------
def test_a_builtin_with_no_file_is_offered(complete):
    assert complete("ali", "") == ("alias ", [])


def test_builtins_and_files_are_listed_together(complete):
    line, options = complete("c", "/system/bin/cat\n/system/bin/chmod\n")
    assert "cd" in options and "cat" in options and "chmod" in options
    assert options == sorted(options) and line is None


def test_a_builtin_that_is_also_a_file_is_listed_once(complete):
    _line, options = complete("pw", "/system/bin/pwd\n")
    assert options == [] and _line == "pwd "


def test_a_builtin_after_a_separator(complete):
    assert complete("ls; expor", "") == ("ls; export ", [])


# ---- the list ----------------------------------------------------------------
def test_the_list_shows_the_last_part_of_a_path(complete):
    from turboadb.gui.console import AnsiConsole

    _line, options = complete("ls /da", "/data/\n/data_mirror/\n/date.txt\n")
    text = AnsiConsole._format_columns(options, width=200)
    assert "📁 data/" in text and "📁 data_mirror/" in text and "date.txt" in text
    assert "/data" not in text


def test_a_plain_option_shows_itself():
    from turboadb.gui.console import AnsiConsole, Completion

    assert AnsiConsole._format_columns(["cat"]) == "cat"
    assert AnsiConsole._format_columns(["C:\\Users\\"]) == "📁 C:\\Users\\"
    assert AnsiConsole._format_columns([Completion("a\\ b/", "a b/")]) == "📁 a b/"


def test_a_second_tab_replaces_the_whole_word(qapp):
    """It was found by searching the line for the text after the last space, so
    the second Tab wrote the whole path over only the end of an escaped name."""
    from turboadb.gui.console import AnsiConsole, Completion

    console = AnsiConsole()
    try:
        console._alive = True
        console.set_completion_fn(lambda line: (None, []))
        console._set_line(r"ls /sdcard/My\ F")
        options = [Completion(r"/sdcard/My\ Files/", "My Files/", 3),
                   Completion(r"/sdcard/My\ Folder/", "My Folder/", 3)]
        console._apply_completion_result(None, options)
        console._do_complete()
        assert console._line == r"ls /sdcard/My\ Files/"
        console._do_complete()
        assert console._line == r"ls /sdcard/My\ Folder/"
    finally:
        console.deleteLater()
