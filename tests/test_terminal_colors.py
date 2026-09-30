"""Colour the terminal adds by itself, and cursor moves along one line.

Colour is kept for what matters.  In plain output only the words that say
something went wrong or was refused, needs attention or worked are coloured;
of logcat's lines only warnings, errors and fatal ones, whole, in whatever
pieces they arrive; a prompt the owner recognised in one quiet colour, red
where it means something, and the command after it untouched.  Output with
colours of its own keeps them.  Progress bars that move the cursor along
their line are drawn in place."""

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
