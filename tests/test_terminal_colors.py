"""Colour the terminal adds by itself, and cursor moves along one line.

Colour is kept for what matters.  In plain output only the words that say
something went wrong or was refused, needs attention or worked are coloured;
of logcat's lines only warnings, errors and fatal ones, whole, in whatever
pieces they arrive; a prompt the owner recognised in one quiet colour, red
where it means something, and the command after it untouched.  Output with
colours of its own keeps them.  Progress bars that move the cursor along
their line are drawn in place."""

import time

import pytest

pytest.importorskip("PyQt5")

from PyQt5.QtGui import QColor, QFont  # noqa: E402

from turboadb.gui import theme  # noqa: E402

THREADTIME = "09-29 16:42:56.210  5927  5927 {} Finsky  : message {}"


def _console(pty=True):
    from turboadb.gui.console import AnsiConsole

    term = AnsiConsole(send_fn=lambda _data: None)
    term.set_emulate_prompt(False)
    if pty:
        term.set_screen_input(lambda _data: True)
    term.resize(900, 500)
    return term


def _render(term):
    while term._inq:
        term._drain_tick()


def _close(term):
    term.close_archive()
    term.deleteLater()


def _runs(term, line_no):
    """``(text, colour, bold)`` for each piece of a line of the view."""
    block = term.document().findBlockByNumber(line_no)
    runs = []
    it = block.begin()
    while not it.atEnd():
        fragment = it.fragment()
        if fragment.isValid():
            fmt = fragment.charFormat()
            runs.append((fragment.text(), fmt.foreground().color().name(),
                         fmt.fontWeight() > QFont.Normal))
        it += 1
    return runs


def _color(value):
    return QColor(value).name()


def _chars(term, line_no):
    """``(colour, bold)`` of each character of a line of the view."""
    return [(color, bold) for text, color, bold in _runs(term, line_no) for _ in text]


def _meaning(kind):
    color, bold = theme.TERM_MEANING_COLORS[kind]
    return _color(color), bold


def _plain():
    return _color(theme.TERM_FG), False


def _shown(term, line_no, text):
    """``(colour, bold)`` of *text* where it first is on a line of the view:
    one pair when all of it looks the same, else the set of them."""
    line = term.document().findBlockByNumber(line_no).text()
    start = line.index(text)
    looks = set(_chars(term, line_no)[start:start + len(text)])
    return looks.pop() if len(looks) == 1 else looks


# --------------------------------------------------------------------------- #
# logcat lines
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("level", ["W", "E", "F", "A"])
def test_a_warning_error_or_fatal_logcat_line_takes_its_prioritys_colour(qapp, level):
    term = _console()
    try:
        line = THREADTIME.format(level, 1)
        term.feed(line + "\r\n")
        _render(term)
        color = theme.LOGCAT_LEVELS["F" if level == "A" else level]
        assert _runs(term, 0) == [(line, _color(color), level in "FA")]
    finally:
        _close(term)


@pytest.mark.parametrize("level", ["V", "D", "I"])
def test_a_routine_logcat_line_keeps_the_default_colour(qapp, level):
    """Verbose, debug and info lines are most of logcat: coloured, the whole
    view was colour.  Their words are not coloured either."""
    term = _console()
    try:
        line = THREADTIME.format(level, 1).replace("message", "connection failed, retrying")
        term.feed(line + "\r\n")
        _render(term)
        assert _runs(term, 0) == [(line, _color(theme.TERM_FG), False)]
    finally:
        _close(term)


def test_logcat_lines_cut_anywhere_between_reads_are_coloured_whole(qapp):
    term = _console()
    try:
        text = "".join(THREADTIME.format(lvl, n) + "\r\n" for n, lvl in enumerate("EWIDV"))
        for start in range(0, len(text), 7):  # every line cut in several places
            term.feed(text[start:start + 7])
            _render(term)
        for n, lvl in enumerate("EWIDV"):
            runs = _runs(term, n)
            assert "".join(r[0] for r in runs) == THREADTIME.format(lvl, n)
            color = theme.LOGCAT_LEVELS[lvl] if lvl in "EW" else theme.TERM_FG
            assert {r[1] for r in runs} == {_color(color)}, (n, runs)
    finally:
        _close(term)


def test_a_logcat_line_shows_its_colour_before_it_ends(qapp):
    """A flood's last line is usually still arriving: once its start shows
    the priority, it is drawn in that colour, not in the default one first."""
    term = _console()
    try:
        line = THREADTIME.format("W", 1)
        warn, default = _color(theme.LOGCAT_LEVELS["W"]), _color(theme.TERM_FG)
        term.feed(line[:20])  # no priority yet
        _render(term)
        assert {r[1] for r in _runs(term, 0)} == {default}
        term.feed(line[20:40])
        _render(term)
        assert _runs(term, 0) == [(line[:40], warn, False)]
        term.feed(line[40:50])
        _render(term)
        assert _runs(term, 0) == [(line[:50], warn, False)]
        term.feed(line[50:] + "\r\n" + THREADTIME.format("I", 2)[:36])
        _render(term)
        assert _runs(term, 0) == [(line, warn, False)]
        assert _runs(term, 1) == [(THREADTIME.format("I", 2)[:36], default, False)]
    finally:
        _close(term)


def test_a_line_shown_in_a_logcat_colour_gives_it_back_for_its_own(qapp):
    term = _console()
    try:
        line = THREADTIME.format("E", 1)
        default = _color(theme.TERM_FG)
        term.feed(line[:40])
        _render(term)
        assert {r[1] for r in _runs(term, 0)} == {_color(theme.LOGCAT_LEVELS["E"])}
        term.feed("\x1b[1;31mmatch\x1b[0m rest\r\n")  # grep --color, its match in the next read
        term.feed(line[:40])
        _render(term)
        term.feed("\rprogress\r\n")  # written over from its start
        _render(term)
        runs = _runs(term, 0)
        assert runs[0] == (line[:40], default, False)
        assert [r for r in runs if r[0] == "match"][0][1] != default
        assert runs[-1] == (" rest", default, False)
        assert {r[1] for r in _runs(term, 1)} == {default}
        assert "".join(r[0] for r in _runs(term, 1)) == "progress" + line[8:40]
    finally:
        _close(term)


def test_a_line_with_colours_of_its_own_keeps_them(qapp):
    term = _console()
    try:
        # logcat -v color, and a colour left on by earlier output
        term.feed("\x1b[38;5;196m" + THREADTIME.format("I", 1) + "\x1b[0m\r\n")
        term.feed("\x1b[32m" + "prefix\r\n" + THREADTIME.format("E", 2) + "\r\n\x1b[0m")
        term.feed(THREADTIME.format("W", 3) + "\r\n")
        _render(term)
        assert {r[1] for r in _runs(term, 0)} == {QColor(255, 0, 0).name()}
        assert {r[1] for r in _runs(term, 2)} == {_color(theme.ANSI_FG[32])}  # still green
        assert {r[1] for r in _runs(term, 3)} == {_color(theme.LOGCAT_LEVELS["W"])}
    finally:
        _close(term)


def test_other_output_keeps_the_default_colour(qapp):
    term = _console()
    try:
        term.feed("I/O error: disk full\r\ndrwxr-x--x 2 root root 4096 2026-09-29 16:42 data\r\n"
                  "50%\r75%\r" + THREADTIME.format("E", 1) + "\r\n")
        _render(term)
        # no logcat line: only its word "error" is coloured
        assert _shown(term, 0, "I/O ") == _plain()
        assert _shown(term, 0, "error") == _meaning("error")
        assert _shown(term, 0, ": disk full") == _plain()
        default = _color(theme.TERM_FG)
        for n in (1, 2):
            assert {r[1] for r in _runs(term, n)} == {default}, n  # a CR rewrote the last one
    finally:
        _close(term)


def test_long_format_messages_share_their_headers_colour(qapp):
    term = _console()
    try:
        term.feed("[ 09-29 16:42:56.210  5927: 5927 E/AndroidRuntime ]\r\nFATAL EXCEPTION\r\n"
                  "\tat Main.run\r\n\r\nplain after the entry\r\n")
        _render(term)
        red = _color(theme.LOGCAT_LEVELS["E"])
        for n in range(3):
            assert {r[1] for r in _runs(term, n)} == {red}, n
        assert {r[1] for r in _runs(term, 4)} == {_color(theme.TERM_FG)}
    finally:
        _close(term)


def test_local_shells_colour_logcat_too(qapp):
    """``adb logcat`` typed in PowerShell or CMD (no device terminal)."""
    term = _console(pty=False)
    try:
        term.feed((THREADTIME.format("E", 1) + "\r\r\n").encode())
        _render(term)
        assert {r[1] for r in _runs(term, 0)} == {_color(theme.LOGCAT_LEVELS["E"])}
    finally:
        _close(term)


def test_the_saved_history_has_no_colour_codes(qapp):
    term = _console()
    try:
        term.feed(THREADTIME.format("E", 1) + "\r\n")
        term.feed("V2318:/ $ ")
        term.mark_prompt()
        _render(term)
        saved = term._sb.full_text()
        assert THREADTIME.format("E", 1) in saved and saved.endswith("V2318:/ $ ")
        assert "\x1b" not in saved
    finally:
        _close(term)


# --------------------------------------------------------------------------- #
# the device prompt
# --------------------------------------------------------------------------- #
def _prompt_colors(*parts):
    return [(text, _color(theme.TERM_PROMPT_COLORS[part][0]), theme.TERM_PROMPT_COLORS[part][1])
            for text, part in parts]


def _prompt_look(part):
    color, bold = theme.TERM_PROMPT_COLORS[part]
    return _color(color), bold


def test_a_prompt_the_owner_recognised_is_coloured_part_by_part(qapp):
    term = _console()
    try:
        term.feed("130|V2318:/sdcard/My Folder # ")
        term.mark_prompt()
        _render(term)
        runs = _runs(term, 0)
        default = _color(theme.TERM_FG)
        assert runs == _prompt_colors(
            ("130|", "status"), ("V2318", "host"), (":", "colon"), ("/sdcard/My Folder", "path"),
        ) + [(" ", default, False)] + _prompt_colors(("#", "root")) + [(" ", default, False)]
    finally:
        _close(term)


def test_a_prompt_cut_between_reads_is_coloured_once_it_is_complete(qapp):
    term = _console()
    try:
        term.feed("shell@V23")
        _render(term)
        term.feed("18:/ $ ")
        term.mark_prompt()
        _render(term)
        assert term.document().firstBlock().text() == "shell@V2318:/ $ "
        looks = [_prompt_look("user")] * 6 + [_prompt_look("host")] * 5
        looks += [_prompt_look("colon"), _prompt_look("path"), _plain(), _prompt_look("mark"), _plain()]
        assert _chars(term, 0) == looks
    finally:
        _close(term)


def test_the_command_after_the_prompt_is_not_coloured(qapp):
    term = _console()
    try:
        term.feed("V2318:/ $ ")
        term.mark_prompt()
        _render(term)
        term._submit_line("echo $ HOME")  # drawn after the prompt, echo hidden
        _render(term)
        runs = _runs(term, 0)
        mark = next(i for i, run in enumerate(runs) if run[0] == "$")
        after = runs[mark + 1:]
        assert "".join(run[0] for run in after) == " echo $ HOME"
        assert {run[1:] for run in after} == {(_color(theme.TERM_FG), False)}
    finally:
        _close(term)


def test_a_line_that_only_looks_like_a_prompt_is_left_alone(qapp):
    term = _console()
    try:
        term.feed("V2318:/ $ in the middle of output\r\nV2318:/data $ \r\nmore\r\n")
        _render(term)
        default = _color(theme.TERM_FG)
        for n in range(3):
            assert {r[1] for r in _runs(term, n)} == {default}  # nobody said "prompt"
    finally:
        _close(term)


def test_a_prompt_with_an_emoji_in_its_path_is_coloured_to_its_end(qapp):
    term = _console()
    try:
        term.feed("1|host:/sdcard/📁 $ ")
        term.mark_prompt()
        _render(term)
        default = _color(theme.TERM_FG)
        assert _runs(term, 0) == _prompt_colors(
            ("1|", "status"), ("host", "host"), (":", "colon"), ("/sdcard/📁", "path"),
        ) + [(" ", default, False)] + _prompt_colors(("$", "mark")) + [(" ", default, False)]
    finally:
        _close(term)


def test_a_prompt_with_colours_of_its_own_keeps_them(qapp):
    term = _console()
    try:
        term.feed("\x1b[1;34mV2318:/ $ \x1b[0m")
        term.mark_prompt()
        _render(term)
        assert {r[1] for r in _runs(term, 0)} == {_color(theme.ANSI_FG[34])}
    finally:
        _close(term)


def test_the_pipe_prompt_keeps_its_own_colours(qapp):
    """Over a pipe the console draws the prompt itself, as before."""
    from turboadb.gui.console import AnsiConsole

    term = AnsiConsole(send_fn=lambda _data: None)  # draws its own prompt
    try:
        term.set_prompt("shell@V2318")
        term.set_alive(True)
        assert _shown(term, 0, "shell@") == _prompt_look("user")
        assert _shown(term, 0, "V2318") == _prompt_look("host")
        assert _shown(term, 0, ":") == _prompt_look("colon")
        assert _shown(term, 0, "$") == _prompt_look("mark")
    finally:
        _close(term)


def test_every_prompt_is_drawn_in_one_quiet_colour(qapp):
    """A device shell's own prompt, TurboADB's over a pipe and a local
    shell's look alike: no rainbow, and red only for a failed command's exit
    status and root's "#"."""
    from turboadb.gui.console import AnsiConsole

    quiet = _color(theme.TERM_PROMPT), False
    for part in ("user", "host", "path", "mark"):
        assert _prompt_look(part) == quiet, part
    red = _color(theme.ANSI_FG[91]), True
    assert _prompt_look("status") == red and _prompt_look("root") == red
    term = _console(pty=False)
    try:
        term.feed(AnsiConsole._style_local_prompts("PS C:\\Users\\dev> "))
        _render(term)
        prompt = "PS C:\\Users\\dev>"
        assert _shown(term, 0, prompt) == quiet
    finally:
        _close(term)


# the invisible mark the PowerShell and CMD terminals end every prompt with
PS_MARK = "\x1b]7717;C:\\w\x07"
CMD_MARK = "\x1b]7717;C:\\w\x1b\\"


@pytest.mark.parametrize("line, word", [
    ("e: C:\\proj\\app\\src\\Foo.kt: (12, 5): Type mismatch: inferred type is List<String> but Set<String>",
     None),
    ("E: mount: /data -> Permission denied", "Permission denied"),
    ("u:r:zygote:s0 pid 412 <pre-initialized> failed", "failed"),
    ("C:\\proj\\src\\main.c(12): warning: conversion 'int' -> 'char'", "warning"),
    ("PS aux > /sdcard/ps.txt: Permission denied", "Permission denied"),
], ids=["kotlin", "mount", "selinux", "compiler", "ps"])
@pytest.mark.parametrize("pty", [False, True], ids=["local", "device"])
def test_output_that_only_looks_like_a_local_prompt_keeps_its_look(qapp, pty, line, word):
    """A line starting with a letter and a colon was drawn as cmd's prompt up
    to its first ">", and had none of its words coloured."""
    term = _console(pty)
    try:
        term.feed("first\r\n" + line + "\r\nlast\r\n")
        term.feed(line)  # ... also where a read ends in the middle of such a line
        term.feed(" and more\r\n")
        _render(term)
        quiet = _color(theme.TERM_PROMPT)
        for n in (1, 3):
            looks = {run[1] for run in _runs(term, n)}
            assert quiet not in looks, _runs(term, n)
        if word is None:
            assert set(_chars(term, 1)) == {_plain()}
        else:
            assert _shown(term, 1, word) == _meaning("warning" if word == "warning" else "error")
    finally:
        _close(term)


@pytest.mark.parametrize("output, prompt, lines", [
    ("dir\r\n file.txt\r\nPS C:\\w> " + PS_MARK, "PS C:\\w>", [2]),          # PowerShell's
    ("dir\r\n file.txt\r\n\r\nC:\\w>" + CMD_MARK, "C:\\w>", [3]),            # cmd's
    ("cmd\r\nMicrosoft Windows\r\n\r\nC:\\w>", "C:\\w>", [3]),               # a shell in the shell: no mark
    ("PS C:\\w> " + PS_MARK + "dir\r\n file.txt\r\nPS C:\\w> " + PS_MARK,   # ... and the echo of a line
     "PS C:\\w>", [0, 2]),                                                   # typed ahead in the same read
], ids=["powershell", "cmd", "nested", "typed ahead"])
def test_a_local_shells_own_prompt_is_still_coloured(qapp, output, prompt, lines):
    term = _console(pty=False)
    try:
        term.feed(output)
        _render(term)
        quiet = _color(theme.TERM_PROMPT), False
        for n in lines:
            assert _shown(term, n, prompt) == quiet, n
        assert _shown(term, 1, "file.txt" if "file.txt" in output else "Microsoft") == _plain()
        if len(lines) == 2:
            assert _shown(term, 0, "dir") == _plain()  # the command after a prompt
    finally:
        _close(term)


def test_a_prompt_whose_mark_comes_in_the_next_read_is_coloured_too(qapp):
    term = _console(pty=False)
    try:
        term.feed("done\r\nPS C:\\w> ")
        term.feed(PS_MARK)
        _render(term)
        assert _shown(term, 1, "PS C:\\w>") == (_color(theme.TERM_PROMPT), False)
    finally:
        _close(term)


@pytest.mark.parametrize("prompt, mark", [("PS C:\\w> ", PS_MARK), ("C:\\w>", CMD_MARK)],
                         ids=["powershell", "cmd"])
@pytest.mark.parametrize("cut", [1, 2, 9])
def test_a_prompt_whose_mark_is_cut_by_the_read_is_coloured_too(qapp, prompt, mark, cut):
    """A read that ended after the first byte of the mark left the prompt in
    front of it in the default colour (a read ends anywhere in 4 KB of output)."""
    term = _console(pty=False)
    try:
        term.feed("done\r\n" + prompt + mark[:cut])
        term.feed(mark[cut:] + "dir\r\n")
        _render(term)
        assert term.toPlainText() == "done\n" + prompt + "dir\n"
        assert _shown(term, 1, prompt.strip()) == (_color(theme.TERM_PROMPT), False)
        assert _shown(term, 1, "dir") == _plain()
        assert term._sb.full_text() == "done\n" + prompt + "dir\n"
    finally:
        _close(term)


def test_a_device_terminal_has_no_local_prompt_to_colour(qapp):
    """What a device prints is never PowerShell's or cmd's prompt, whatever
    it looks like: a device shell's own prompt is coloured when its owner
    recognises it (mark_prompt)."""
    term = _console(pty=True)
    try:
        term.feed("cat notes.txt\r\nPS C:\\w> ")
        _render(term)
        assert set(_chars(term, 1)) == {_plain()}
        term.feed("\r\nV2318:/ $ ")
        term.mark_prompt()
        _render(term)
        assert _shown(term, 2, "V2318") == _prompt_look("host")
    finally:
        _close(term)


# --------------------------------------------------------------------------- #
# the words that say what happened
# --------------------------------------------------------------------------- #
def test_only_the_words_that_say_what_happened_are_coloured(qapp):
    term = _console()
    try:
        term.feed("ls: /data/system: Permission denied\r\n"
                  "Warning: deprecated option -r\r\n"
                  "/sdcard/a.mp4: 1 file pushed, 0 skipped.\r\n"
                  "-rw-r--r-- 1 root root 220 2026-09-30 10:00 error.log\r\n")
        _render(term)
        assert _shown(term, 0, "ls: /data/system: ") == _plain()
        assert _shown(term, 0, "Permission denied") == _meaning("error")
        assert _shown(term, 1, "Warning") == _meaning("warning")
        assert _shown(term, 1, ": ") == _plain()
        assert _shown(term, 1, "deprecated") == _meaning("warning")
        assert _shown(term, 1, " option -r") == _plain()
        assert _shown(term, 2, "pushed") == _meaning("success")
        assert _shown(term, 2, ", 0 skipped.") == _plain()
        assert set(_chars(term, 3)) == {_plain()}  # a file's name
        red, amber, green = (_meaning(k)[0] for k in ("error", "warning", "success"))
        assert len({red, amber, green, _color(theme.TERM_FG)}) == 4
    finally:
        _close(term)


def test_a_line_cut_between_reads_has_its_words_coloured_once_it_ends(qapp):
    term = _console(pty=False)
    try:
        line = "cat: /sdcard/x.txt: No such file or directory"
        for start in range(0, len(line), 5):
            term.feed(line[start:start + 5])
            _render(term)
        term.feed("\r\r\n")
        _render(term)
        assert term.document().firstBlock().text() == line
        assert _shown(term, 0, "cat: /sdcard/x.txt: ") == _plain()
        assert _shown(term, 0, "No such file or directory") == _meaning("error")
    finally:
        _close(term)


def test_a_long_line_has_its_words_coloured_too(qapp):
    term = _console()
    try:
        line = "x " * 200 + "failed"
        term.feed(line[:300])
        _render(term)
        term.feed(line[300:] + "\r\n")
        _render(term)
        assert _shown(term, 0, "failed") == _meaning("error")
        assert _shown(term, 0, "x x") == _plain()
    finally:
        _close(term)


def test_a_line_with_colours_of_its_own_has_no_words_coloured(qapp):
    term = _console()
    try:
        term.feed("\x1b[32mbuild\x1b[0m failed\r\n")
        _render(term)
        assert _shown(term, 0, "build") == (_color(theme.ANSI_FG[32]), False)
        assert _shown(term, 0, " failed") == _plain()
    finally:
        _close(term)


def test_words_after_an_emoji_are_coloured_where_they_are(qapp):
    """A word's place counts characters, the view's positions count an emoji
    twice: the colour began a character early for every emoji before it."""
    term = _console()
    try:
        term.feed("🔥 build failed\r\n🚀🚀 Done, 0 errors\r\n")
        _render(term)
        assert _shown(term, 0, "🔥 build ") == _plain()
        assert _shown(term, 0, "failed") == _meaning("error")
        assert _shown(term, 1, "🚀🚀 ") == _plain()
        assert _shown(term, 1, "Done") == _meaning("success")
        assert _shown(term, 1, ", ") == _plain()
        assert _shown(term, 1, "0 errors") == _meaning("success")
    finally:
        _close(term)


def test_a_word_cased_for_a_turkish_locale_never_stops_the_drawing(qapp):
    """Java's toLowerCase() makes "faıled" of FAILED there: looking up what
    it says raised KeyError, and the lines after it were never drawn."""
    term = _console()
    try:
        term.feed("E: Install faıled with error 5\r\nline after 1\r\n"
                  "ERROR: PERMİSSİON DENİED\r\nline after 2\r\n")
        _render(term)
        assert term.toPlainText() == ("E: Install faıled with error 5\nline after 1\n"
                                      "ERROR: PERMİSSİON DENİED\nline after 2\n")
        assert _shown(term, 0, "faıled") == _meaning("error")
        assert _shown(term, 0, " with ") == _plain()
        assert _shown(term, 2, "PERMİSSİON DENİED") == _meaning("error")
    finally:
        _close(term)


def test_the_prompt_and_the_command_after_it_have_no_words_coloured(qapp):
    term = _console()
    try:
        term.feed("V2318:/ $ ")
        term.mark_prompt()
        _render(term)
        term.feed("echo failed\r\r\nfailed\r\r\n")  # the device's echo, then the output
        _render(term)
        assert _shown(term, 0, "echo failed") == _plain()
        assert _shown(term, 1, "failed") == _meaning("error")
    finally:
        _close(term)


# --------------------------------------------------------------------------- #
# cursor moves along one line
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("text, shown", [
    ("50%\x1b[10Gx\x1b[3Dy", "50%    y x"),                # CHA past the end, CUB
    ("abcdef\x1b[3G\x1b[2X", "ab  ef"),                  # ECH
    ("abcdef\x1b[2G\x1b[2P", "adef"),                    # DCH
    ("abcdef\x1b[2G\x1b[2@X", "aX bcdef"),               # ICH, the cursor stays
    ("abc\x1b[2Cd", "abc  d"),                           # CUF past the end
    ("abc\x1b[2Cd\r\n", "abc  d"),                       # ...with its line break in the same read
    ("abc\x1b[2C\r\n", "abc"),                           # a move alone leaves no blanks
    ("abc\x1b[1`Z", "Zbc"),                              # HPA
    ("abc\x1b[2aZ", "abc  Z"),                           # HPR
    ("[    ] 0%\r[#\x1b[3C] 25%", "[#   ] 25%"),         # a progress bar redrawn
    ("abc\x1b7XY\x1b8Z", "abcZY"),                        # ESC 7 / 8 on one line
    ("abc\x1b[sXY\x1b[uZ", "abcZY"),                      # CSI s / u on one line
    ("ab\x1b[999Cc", None),                              # clamped to the view's width
])
def test_cursor_moves_along_the_line(qapp, text, shown):
    term = _console()
    try:
        term.feed(text)
        _render(term)
        line = term.document().firstBlock().text()
        if shown is None:
            cols = term.cell_size()[0]
            assert line == "ab" + " " * (cols - 3) + "c"
        else:
            assert line == shown
    finally:
        _close(term)


def test_moves_also_work_on_a_pipe(qapp):
    term = _console(pty=False)
    try:
        term.feed("abc\x1b[5Gx\x1b[1;1Hignored")  # no screen over a pipe: CUP is dropped
        _render(term)
        assert term.document().firstBlock().text() == "abc xignored"
        assert not term.has_screen()
    finally:
        _close(term)


@pytest.mark.parametrize("text, shown", [
    ("🚀🚀🚀 10%\rab", "ab🚀🚀 10%"),                              # two positions: one emoji
    ("🚀🚀🚀 10%\ra", "a🚀🚀 10%"),                                # never half of one
    ("cafe\N{COMBINING ACUTE ACCENT}!\rCAFE", "CAFE!"),           # a letter goes with its accent
    ("ab🚀\bX", "abX"),                                           # a backspace: back over all of it
    ("ab🚀cd\x1b[4G\x1b[1X", "ab  cd"),                           # ECH
    ("ab🚀cd\x1b[3G\x1b[1P", "abcd"),                             # DCH
    ("🚀🚀x\x1b[2G\x1b[1K", "  🚀x"),                              # EL 1
    ("🚀🚀x\x1b[2GZ", "Z🚀x"),                                    # a column inside an emoji is its start
], ids=["two", "one", "accent", "backspace", "ech", "dch", "el1", "column"])
def test_moves_and_overwrites_take_whole_characters(qapp, text, shown):
    """An emoji is two positions of the document and a letter with a joined
    accent is two: none of them is ever cut in half."""
    term = _console()
    try:
        term.feed(text)
        _render(term)
        assert term.document().firstBlock().text() == shown
    finally:
        _close(term)


@pytest.mark.parametrize("text, shown", [
    ("processor\t: 0", "processor       : 0"),              # /proc/cpuinfo: its colons in column 16
    ("BogoMIPS\t: 38.40", "BogoMIPS        : 38.40"),       # (a stop is always ahead of the cursor)
    ("CPU implementer\t: 0x41", "CPU implementer : 0x41"),
    ("\tat Main.run", "        at Main.run"),
    ("a\tb\tc", "a       b       c"),
    ("ab\x1b[3C\tx", "ab      x"),                          # from where a move left the cursor
    ("\x1b[32mgreen\x1b[0m\tplain", "green   plain"),       # ... or text drawn before
    ("🚀\tx", "🚀      x"),                                  # an emoji is two columns wide
    ("0123456789\rab\tcd", "ab234567cd"),                   # over a line: a move, nothing is erased
    ("0123\rab\tcd", "ab23    cd"),                         # ... and blanks past its end
    ("0123456789\r\tZ", "01234567Z9"),
], ids=["cpuinfo", "stop ahead", "one blank", "start", "two", "after a move", "after colour", "emoji",
        "over", "over and past", "over from the start"])
@pytest.mark.parametrize("pieces", [1, 2, 3])
def test_a_tab_moves_to_the_next_tab_stop(qapp, text, shown, pieces):
    """A tab was four blanks wherever it stood: columns a terminal lines up
    (``cat /proc/cpuinfo``) were ragged, and over a line it wiped four
    characters.  The stops are eight columns apart, as on a program's screen."""
    term = _console()
    try:
        size = -(-len(text) // pieces)
        for start in range(0, len(text), size):  # in one read, or cut in two or three
            term.feed(text[start:start + size])
            _render(term)
        assert term.document().firstBlock().text() == shown
    finally:
        _close(term)


def test_tabs_line_up_in_every_line_of_a_read(qapp):
    term = _console(pty=False)
    try:
        term.feed("processor\t: 0\r\nBogoMIPS\t: 38.40\r\nCPU implementer\t: 0x41\r\n"
                  "Features\t: fp asimd\r\n\r\nx\ty\r\n")
        _render(term)
        assert term.toPlainText() == ("processor       : 0\nBogoMIPS        : 38.40\n"
                                      "CPU implementer : 0x41\nFeatures        : fp asimd\n\n"
                                      "x       y\n")
    finally:
        _close(term)


@pytest.mark.parametrize("pty", [True, False])
def test_a_line_of_many_tabs_costs_no_more_than_its_length(qapp, pty):
    """Every tab counted its line's text again from the start: a table of
    tab-separated values was drawn ten to seventy times as slowly as with
    four blanks a tab, and a few lines of a thousand columns held the window."""
    term = _console(pty)
    try:
        row = "\t".join(str(n % 10) for n in range(501))
        started = time.perf_counter()
        term.feed((row + "\r\n") * 100)
        _render(term)
        assert time.perf_counter() - started < 2.0
        shown = "".join(f"{n % 10:<8}" for n in range(500)) + "0"
        assert term.toPlainText() == (shown + "\n") * 100
    finally:
        _close(term)


# "loading" in Arabic and in Hebrew, written from right to left
ARABIC = "".join(map(chr, (0x62A, 0x62D, 0x645, 0x64A, 0x644)))
HEBREW = "".join(map(chr, (0x5D8, 0x5D5, 0x5E2, 0x5DF)))


@pytest.mark.parametrize("word", [ARABIC, HEBREW], ids=["arabic", "hebrew"])
def test_a_right_to_left_line_is_written_over_in_place(qapp, word):
    """A cursor step to the right goes back in a right-to-left line: its
    progress was never written over, the end of the line above went
    instead, and a spinner grew by a character for every backspace."""
    term = _console()
    try:
        term.feed("previous line of output\r\n" + word + " 10%")
        _render(term)
        term.feed("\r" + word + " 20%")
        _render(term)
        assert term.toPlainText() == "previous line of output\n" + word + " 20%"
        term.feed("\r\n" + word + " |")
        for frame in "/-\\":
            term.feed("\b" + frame)
            _render(term)
        assert term.toPlainText() == "previous line of output\n" + word + " 20%\n" + word + " \\"
    finally:
        _close(term)
