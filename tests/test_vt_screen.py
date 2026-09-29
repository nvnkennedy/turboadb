"""The terminal's screen model for full-screen programs (pure Python, no Qt).

``top``, ``watch``, ``vi`` and progress displays that move back up address
the cursor; the console hands their output to :class:`vtscreen.Screen`,
which keeps a grid of cells the way a VT100/xterm does.  These tests pin
the grid after each kind of sequence, and that a stream gives the same
screen however it is cut into reads."""

import random
import time

import pytest

from turboadb.gui import vtscreen
from turboadb.gui.vtscreen import (
    BLINK, BOLD, DEFAULT, DIM, INVISIBLE, ITALIC, PLAIN, REVERSE, STRIKE, TRUECOLOR,
    UNDERLINE, Screen, char_width,
)


def screen(rows=5, cols=10, text=""):
    replies = []
    s = Screen(rows, cols, reply=replies.append)
    s.replies = replies
    if text:
        s.feed(text)
    return s


def state(s):
    """Everything a screen shows, for comparing two screens."""
    return (
        [(list(line.chars), list(line.attrs), line.wrapped) for line in s.lines],
        s.cursor, s.wrap_pending, s.alt_active, s.attr,
    )


# --------------------------------------------------------------------------- #
# printing, wrapping and the C0 controls
# --------------------------------------------------------------------------- #
def test_text_goes_in_at_the_cursor():
    s = screen(text="hello")
    assert s.display() == ["hello", "", "", "", ""]
    assert s.cursor == (0, 5)


def test_cr_lf_backspace_and_tab():
    s = screen(text="abc\rX\nY\bZ\tT")
    assert s.display()[0] == "Xbc"
    # LF moves down only (the device's terminal turns \n into \r\n itself)
    assert s.display()[1] == " Z      T"
    assert s.cursor == (1, 9)


def test_backspace_stops_at_the_left_edge():
    s = screen(text="\b\b\bA")
    assert s.display()[0] == "A"


def test_vt_and_ff_move_down_like_lf():
    s = screen(text="a\x0bb\x0cc")
    assert s.display()[:3] == ["a", " b", "  c"]


def test_newline_mode_makes_lf_return_too():
    s = screen(text="\x1b[20hab\ncd")
    assert s.display()[:2] == ["ab", "cd"]
    s.feed("\x1b[20l\rxy\nz")
    assert s.display()[1:3] == ["xy", "  z"]


def test_the_last_column_waits_to_wrap():
    s = screen(rows=3, cols=5, text="abcde")
    assert s.cursor == (0, 4) and s.wrap_pending
    s.feed("f")
    assert s.display()[:2] == ["abcde", "f"]
    assert s.lines[0].wrapped and not s.lines[1].wrapped


def test_a_cr_at_the_last_column_cancels_the_wrap():
    s = screen(rows=3, cols=5, text="abcde\rX")
    assert s.display()[:2] == ["Xbcde", ""]


def test_without_auto_wrap_the_last_column_is_overwritten():
    s = screen(rows=3, cols=5, text="\x1b[?7labcdefgh")
    assert s.display() == ["abcdh", "", ""]
    s.feed("\x1b[?7h\rabcdefg")
    assert s.display()[:2] == ["abcde", "fg"]


def test_wrapping_at_the_bottom_scrolls():
    s = screen(rows=2, cols=3, text="abcdefgh")
    assert s.display() == ["def", "gh"]
    assert [line.text() for line in s.take_scrolled_off()] == ["abc"]
    assert s.take_scrolled_off() == []


def test_bell_is_counted_and_other_controls_are_ignored():
    s = screen(text="a\x07b\x00c\x01\x7f\x85d")
    assert s.display()[0] == "abcd"
    assert s.bell == 1


# --------------------------------------------------------------------------- #
# cursor movement
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("seq, cursor", [
    ("\x1b[3;4H", (2, 3)),
    ("\x1b[3;4f", (2, 3)),
    ("\x1b[H", (0, 0)),
    ("\x1b[;5H", (0, 4)),
    ("\x1b[99;99H", (4, 9)),
    ("\x1b[2;2H\x1b[A", (0, 1)),
    ("\x1b[2;2H\x1b[5A", (0, 1)),
    ("\x1b[2;2H\x1b[2B", (3, 1)),
    ("\x1b[2;2H\x1b[9B", (4, 1)),
    ("\x1b[2;2H\x1b[3C", (1, 4)),
    ("\x1b[2;2H\x1b[99C", (1, 9)),
    ("\x1b[2;5H\x1b[2D", (1, 2)),
    ("\x1b[2;5H\x1b[99D", (1, 0)),
    ("\x1b[2;5H\x1b[2E", (3, 0)),
    ("\x1b[4;5H\x1b[2F", (1, 0)),
    ("\x1b[2;5H\x1b[7G", (1, 6)),
    ("\x1b[2;5H\x1b[7`", (1, 6)),
    ("\x1b[2;5H\x1b[3a", (1, 7)),
    ("\x1b[2;5H\x1b[4d", (3, 4)),
    ("\x1b[2;5H\x1b[2e", (3, 4)),
    ("\x1b[2;5H\x1b[0A", (0, 4)),  # 0 means 1
])
def test_cursor_movement(seq, cursor):
    s = screen(text=seq)
    assert s.cursor == cursor


def test_a_move_cancels_a_pending_wrap():
    s = screen(rows=3, cols=5, text="abcde\x1b[DX")
    assert s.display()[:2] == ["abcXe", ""]


def test_origin_mode_addresses_the_scroll_region():
    s = screen(rows=6, cols=10, text="\x1b[2;5r\x1b[?6h")
    assert s.cursor == (1, 0)  # home is the region's top
    s.feed("\x1b[2;3H")
    assert s.cursor == (2, 2)
    s.feed("\x1b[9;1H")
    assert s.cursor == (4, 0)  # clamped to the region
    s.feed("\x1b[6n")
    assert s.replies[-1] == "\x1b[4;1R"  # reported relative to the region
    s.feed("\x1b[?6l")
    assert s.cursor == (0, 0)


def test_cursor_up_and_down_stop_at_the_margins_inside_a_region():
    s = screen(rows=6, cols=10, text="\x1b[2;4r\x1b[3;1H\x1b[9A")
    assert s.cursor == (1, 0)
    s.feed("\x1b[9B")
    assert s.cursor == (3, 0)
    s.feed("\x1b[6;1H\x1b[9A")  # below the region: stops at its bottom? no: at the top
    assert s.cursor == (1, 0)


# --------------------------------------------------------------------------- #
# erasing, inserting and deleting
# --------------------------------------------------------------------------- #
def fill(s):
    for row in range(s.rows):
        s.feed(f"\x1b[{row + 1};1H" + "".join(chr(ord("a") + (row + c) % 26) for c in range(s.cols)))


def test_erase_display_below_above_and_all():
    s = screen(rows=3, cols=4)
    fill(s)
    s.feed("\x1b[2;3H\x1b[J")
    assert s.display() == ["abcd", "bc", ""]
    fill(s)
    s.feed("\x1b[2;3H\x1b[1J")
    assert s.display() == ["", "   e", "cdef"]
    fill(s)
    s.feed("\x1b[2J")
    assert s.display() == ["", "", ""]
    assert s.cursor == (2, 3)  # ED leaves the cursor where it was


def test_home_and_erase_below_wipes_the_main_screen():
    s = screen(text="old\r\nlines")
    assert not s.wiped
    s.feed("\x1b[H\x1b[J")
    assert s.wiped and s.display() == [""] * 5
    t = screen(text="x\x1b[2J")
    assert t.wiped and not t.scrollback_cleared
    u = screen(text="x\x1b[3J")
    assert u.wiped and u.scrollback_cleared
    v = screen(text="\x1b[?1049h\x1b[2J\x1b[?1049l")
    assert not v.wiped  # only the alternate screen was erased


def test_erase_line_parts():
    s = screen(rows=1, cols=6, text="abcdef\x1b[3G\x1b[K")
    assert s.display() == ["ab"]
    s = screen(rows=1, cols=6, text="abcdef\x1b[3G\x1b[1K")
    assert s.display() == ["   def"]
    s = screen(rows=1, cols=6, text="abcdef\x1b[3G\x1b[2K")
    assert s.display() == [""]


def test_erase_characters_does_not_shift():
    s = screen(rows=1, cols=8, text="abcdefgh\x1b[3G\x1b[2X")
    assert s.display() == ["ab  efgh"]
    assert s.cursor == (0, 2)


def test_insert_and_delete_characters():
    s = screen(rows=1, cols=6, text="abcdef\x1b[2G\x1b[2@")
    assert s.display() == ["a  bcd"]
    s.feed("\x1b[1G\x1b[3P")
    assert s.display() == ["bcd"]


def test_insert_mode_pushes_the_line_right():
    s = screen(rows=1, cols=6, text="abcdef\x1b[1G\x1b[4hXY\x1b[4lZ")
    assert s.display() == ["XYZbcd"]


def test_erased_cells_keep_the_background_colour():
    s = screen(rows=2, cols=4, text="\x1b[41mab\x1b[K")
    assert s.lines[0].attrs[2] == (DEFAULT, 1, 0)  # red background, nothing else
    assert s.lines[0].attrs[0] == (DEFAULT, 1, 0)
    s.feed("\x1b[0m\x1b[2;1H\x1b[44m\x1b[2K")
    assert all(attr == (DEFAULT, 4, 0) for attr in s.lines[1].attrs)
    assert not s.lines[1].is_blank()


def test_insert_and_delete_lines_inside_the_scroll_region():
    s = screen(rows=5, cols=3)
    for row in range(5):
        s.feed(f"\x1b[{row + 1};1HL{row}")
    s.feed("\x1b[2;4r\x1b[3;2H\x1b[L")
    assert s.display() == ["L0", "L1", "", "L2", "L4"]
    assert s.cursor == (2, 0)
    s.feed("\x1b[2M")
    assert s.display() == ["L0", "L1", "", "", "L4"]
    s.feed("\x1b[5;1H\x1b[L")  # outside the region: nothing
    assert s.display() == ["L0", "L1", "", "", "L4"]


def test_scroll_up_and_down_commands():
    s = screen(rows=4, cols=3)
    for row in range(4):
        s.feed(f"\x1b[{row + 1};1HL{row}")
    s.feed("\x1b[2S")
    assert s.display() == ["L2", "L3", "", ""]
    assert [line.text() for line in s.take_scrolled_off()] == ["L0", "L1"]
    s.feed("\x1b[T")
    assert s.display() == ["", "L2", "L3", ""]
    s.feed("\x1b[1;2;3;4;5T")  # mouse highlight tracking, not a scroll
    assert s.display() == ["", "L2", "L3", ""]


# --------------------------------------------------------------------------- #
# scroll regions
# --------------------------------------------------------------------------- #
def test_lf_at_the_bottom_margin_scrolls_only_the_region():
    s = screen(rows=5, cols=4)
    for row in range(5):
        s.feed(f"\x1b[{row + 1};1HL{row}")
    s.feed("\x1b[2;4r")
    assert s.cursor == (0, 0)  # DECSTBM homes the cursor
    s.feed("\x1b[4;1H\nX")
    assert s.display() == ["L0", "L2", "L3", "X", "L4"]
    assert s.take_scrolled_off() == []  # a region below the top keeps no scrollback


def test_reverse_index_at_the_top_margin_scrolls_down():
    s = screen(rows=4, cols=4)
    for row in range(4):
        s.feed(f"\x1b[{row + 1};1HL{row}")
    s.feed("\x1b[2;3r\x1b[2;1H\x1bMX")
    assert s.display() == ["L0", "X", "L1", "L3"]
    s.feed("\x1b[r\x1b[1;1H\x1bM")  # the whole screen again: down at the top
    assert s.display() == ["", "L0", "X", "L1"]


def test_lf_below_the_region_does_not_scroll():
    s = screen(rows=4, cols=4, text="\x1b[1;2r\x1b[4;1HA\nB")
    assert s.display() == ["", "", "", "AB"]


def test_invalid_regions_are_ignored():
    s = screen(rows=4, cols=4, text="\x1b[3;3r")
    assert (s.top, s.bottom) == (0, 3)
    s.feed("\x1b[2;99r")
    assert (s.top, s.bottom) == (1, 3)
    s.feed("\x1b[r")
    assert (s.top, s.bottom) == (0, 3)


def test_index_next_line_and_reverse_index_escapes():
    s = screen(rows=3, cols=4, text="ab\x1bDc\x1bEd\x1bM\x1bMe")
    assert s.display() == ["ae", "  c", "d"]
    assert s.cursor == (0, 2)


# --------------------------------------------------------------------------- #
# the alternate screen
# --------------------------------------------------------------------------- #
def test_1049_saves_clears_and_restores():
    s = screen(rows=3, cols=6, text="shell$\r\nls")
    s.feed("\x1b[?1049h")
    assert s.alt_active and s.alt_used
    assert s.display() == ["", "", ""]
    s.feed("\x1b[H~\r\n~\x1b[2;3Hvi")
    s.feed("\x1b[?1049l")
    assert not s.alt_active
    assert s.display() == ["shell$", "ls", ""]
    assert s.cursor == (1, 2)
    s.feed("\x1b[?1049h")
    assert s.display() == ["", "", ""]  # cleared again


def test_47_keeps_the_alternate_screen_and_1047_clears_it_on_leaving():
    s = screen(rows=2, cols=4, text="main")
    s.feed("\x1b[?47h\x1b[HALT\x1b[?47l")
    assert s.display() == ["main", ""]
    assert s.cursor == (0, 3)  # 47 switches screens, not cursors
    s.feed("\x1b[?47h")
    assert s.display()[0] == "ALT"  # kept
    s.feed("\x1b[?47l\x1b[?1047h")
    assert s.display()[0] == "ALT"
    s.feed("\x1b[?1047l\x1b[?47h")
    assert s.display() == ["", ""]


def test_1048_saves_and_restores_the_cursor():
    s = screen(text="\x1b[2;3H\x1b[?1048h\x1b[5;5H\x1b[?1048l")
    assert s.cursor == (1, 2)


def test_nothing_scrolls_off_the_alternate_screen():
    s = screen(rows=2, cols=3, text="\x1b[?1049hA\r\nB\r\nC\r\nD")
    assert s.take_scrolled_off() == []
    assert s.display() == ["C", "D"]


# --------------------------------------------------------------------------- #
# SGR
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("seq, attr", [
    ("\x1b[1m", (DEFAULT, DEFAULT, BOLD)),
    ("\x1b[2m", (DEFAULT, DEFAULT, DIM)),
    ("\x1b[3m", (DEFAULT, DEFAULT, ITALIC)),
    ("\x1b[4m", (DEFAULT, DEFAULT, UNDERLINE)),
    ("\x1b[4:3m", (DEFAULT, DEFAULT, UNDERLINE)),
    ("\x1b[4m\x1b[4:0m", PLAIN),
    ("\x1b[5m", (DEFAULT, DEFAULT, BLINK)),
    ("\x1b[7m", (DEFAULT, DEFAULT, REVERSE)),
    ("\x1b[8m", (DEFAULT, DEFAULT, INVISIBLE)),
    ("\x1b[9m", (DEFAULT, DEFAULT, STRIKE)),
    ("\x1b[1;2m\x1b[22m", PLAIN),
    ("\x1b[3;4;5;7;8;9m\x1b[23;24;25;27;28;29m", PLAIN),
    ("\x1b[31m", (1, DEFAULT, 0)),
    ("\x1b[97m", (15, DEFAULT, 0)),
    ("\x1b[42m", (DEFAULT, 2, 0)),
    ("\x1b[104m", (DEFAULT, 12, 0)),
    ("\x1b[31;42m\x1b[39m", (DEFAULT, 2, 0)),
    ("\x1b[31;42m\x1b[49m", (1, DEFAULT, 0)),
    ("\x1b[1;31m\x1b[m", PLAIN),
    ("\x1b[38;5;208m", (208, DEFAULT, 0)),
    ("\x1b[48;5;17m", (DEFAULT, 17, 0)),
    ("\x1b[38:5:208m", (208, DEFAULT, 0)),
    ("\x1b[38;2;1;2;3m", (TRUECOLOR | 0x010203, DEFAULT, 0)),
    ("\x1b[48;2;255;128;0m", (DEFAULT, TRUECOLOR | 0xFF8000, 0)),
    ("\x1b[38:2::10:20:30m", (TRUECOLOR | 0x0A141E, DEFAULT, 0)),
    ("\x1b[38:2:10:20:30m", (TRUECOLOR | 0x0A141E, DEFAULT, 0)),
    ("\x1b[38;5;300m", PLAIN),        # out of range: ignored
    ("\x1b[38;5m", PLAIN),            # a selector without its value
    ("\x1b[38;2;1;1;1;1m", (TRUECOLOR | 0x010101, DEFAULT, BOLD)),  # the 1 after it is bold
    ("\x1b[58;5;3;1m", (DEFAULT, DEFAULT, BOLD)),  # underline colour: skipped
])
def test_sgr(seq, attr):
    s = screen(text=seq + "x")
    assert s.lines[0].attrs[0] == attr


def test_sgr_attributes_follow_the_text():
    s = screen(text="a\x1b[1;32mb\x1b[0mc")
    assert s.lines[0].attrs[:3] == [PLAIN, (2, DEFAULT, BOLD), PLAIN]


# --------------------------------------------------------------------------- #
# queries and modes
# --------------------------------------------------------------------------- #
def test_status_and_cursor_position_reports():
    s = screen(rows=24, cols=80, text="\x1b[5n\x1b[12;40H\x1b[6n\x1b[?6n")
    assert s.replies == ["\x1b[0n", "\x1b[12;40R", "\x1b[?12;40R"]


def test_a_cursor_report_needs_nobody_listening():
    s = Screen(3, 3)
    s.feed("\x1b[6n")  # no reply callback: nothing happens


def test_device_attributes_and_size_reports():
    s = screen(rows=24, cols=80, text="\x1b[c\x1b[0c\x1b[>c\x1b[18t\x1b[14t")
    assert s.replies == ["\x1b[?1;2c", "\x1b[?1;2c", "\x1b[8;24;80t"]


def test_the_terminal_size_probe_of_toybox():
    """toybox's terminal_probesize: save, jump far right and down, ask, restore."""
    s = screen(rows=31, cols=97, text="\x1b[3;4H\x1b[s\x1b[999C\x1b[999B\x1b[6n\x1b[u")
    assert s.replies == ["\x1b[31;97R"]
    assert s.cursor == (2, 3)


def test_modes_a_full_screen_program_sets():
    s = screen()
    assert not s.app_cursor_keys and s.cursor_visible and not s.bracketed_paste
    s.feed("\x1b[?1h\x1b[?25l\x1b[?2004h\x1b=")
    assert s.app_cursor_keys and not s.cursor_visible and s.bracketed_paste and s.app_keypad
    s.feed("\x1b[?1l\x1b[?25h\x1b[?2004l\x1b>")
    assert not s.app_cursor_keys and s.cursor_visible and not s.bracketed_paste
    assert not s.app_keypad
    s.feed("\x1b[?1;25;2004h")  # several at once
    assert s.app_cursor_keys and s.bracketed_paste


@pytest.mark.parametrize("seq, shape", [
    ("\x1b[ q", "block"), ("\x1b[2 q", "block"), ("\x1b[4 q", "underline"),
    ("\x1b[6 q", "bar"), ("\x1b[5 q", "bar"),
])
def test_cursor_shape(seq, shape):
    assert screen(text=seq).cursor_shape == shape


def test_soft_reset():
    s = screen(text="\x1b[2;3r\x1b[?6h\x1b[4h\x1b[?25l\x1b[1m\x1b[!p")
    assert (s.top, s.bottom) == (0, 4)
    assert not s.origin_mode and not s.insert_mode and s.cursor_visible
    assert s.attr == PLAIN


def test_full_reset_clears_everything():
    s = screen(text="\x1b[?1049h\x1b[31mabc\x1bc")
    assert not s.alt_active and s.attr == PLAIN and s.display() == [""] * 5
    assert s.cursor == (0, 0) and s.wiped


def test_save_and_restore_cursor_with_attributes():
    s = screen(text="\x1b[2;3H\x1b[1m\x1b7\x1b[5;5H\x1b[0mx\x1b8y")
    assert s.cursor == (1, 3)
    assert s.lines[1].attrs[2] == (DEFAULT, DEFAULT, BOLD)
    t = screen(text="\x1b[2;3H\x1b[s\x1b[H\x1b[u")
    assert t.cursor == (1, 2)
    u = screen(text="\x1b[3;3H\x1b8")  # nothing saved: home
    assert u.cursor == (0, 0)


def test_line_drawing_charset_and_shifts():
    s = screen(text="\x1b(0lqk\x1b(Bq")
    assert s.display()[0] == "┌─┐q"
    t = screen(text="\x1b)0a\x0eq\x0fq")
    assert t.display()[0] == "a─q"


def test_alignment_pattern():
    s = screen(rows=2, cols=3, text="\x1b#8")
    assert s.display() == ["EEE", "EEE"]


def test_tab_stops():
    s = screen(rows=1, cols=30, text="\tA")
    assert s.cursor == (0, 9)
    s.feed("\r\x1b[3g\x1b[5G\x1bH\r\tB")
    assert s.lines[0].chars[4] == "B"
    s.feed("\x1b[20G\x1b[Z")
    assert s.cursor == (0, 4)
    s.feed("\x1b[0g\r\t")
    assert s.cursor == (0, 29)  # no stops left: the last column
    s.feed("\x1bc\x1b[2I")
    assert s.cursor == (0, 16)


def test_a_huge_tab_count_is_quick():
    """ESC[1000000000I stepped from stop to stop a billion times: the screen
    froze for a minute.  More steps than columns only stay at the edge."""
    s = screen(rows=1, cols=40)
    started = time.monotonic()
    s.feed("\x1b[1000000000Ia\x1b[1000000000Zb")
    assert time.monotonic() - started < 2.0
    assert s.display() == ["b" + " " * 38 + "a"]


def test_repeat_the_last_character():
    s = screen(rows=1, cols=10, text="ab\x1b[3b")
    assert s.display() == ["abbbb"]


# --------------------------------------------------------------------------- #
# strings and sequences split across reads
# --------------------------------------------------------------------------- #
def test_osc_dcs_and_other_strings_are_ignored():
    s = screen(text="a\x1b]0;title\x07b\x1b]2;t\x1b\\c\x1bPq#0\x1b\\d\x1b_apc\x1b\\e\x1b^pm\x1b\\f")
    assert s.display()[0] == "abcdef"


def test_an_osc_ended_by_another_escape():
    s = screen(text="\x1b]0;title\x1b[1mX")
    assert s.display()[0] == "X" and s.lines[0].attrs[0][2] == BOLD


def test_a_sequence_cut_between_reads_is_completed():
    s = screen()
    s.feed("ab\x1b")
    s.feed("[1")
    s.feed(";3")
    s.feed("1mc\x1b]0;ti")
    s.feed("tle\x07d")
    assert s.display()[0] == "abcd"
    assert s.lines[0].attrs[2] == (1, DEFAULT, BOLD)


STREAM = (
    "\x1b[?1049h\x1b[H\x1b[2J\x1b[1;32mtop\x1b[0m - 12:00\r\n"
    "\x1b[7m  PID USER\x1b[0m\r\n\x1b[1m  12 shell\x1b[m\r\x1b[5;3H\x1b[K\x1b(0lqqk\x1b(B"
    "\x1b[2;4r\x1b[4;1H\n\n\x1bM\x1b[r\x1b[2@\x1b[3P\x1b[38;2;1;2;3mRGB\x1b[48;5;200m\x1b[K"
    "\x1b]0;x\x07\x1b[6n\t\x1b[3;1H漢字é\x1b[?1049l\x1b[31mmain\x1b[0m\r\nend"
)


def test_a_stream_gives_the_same_screen_however_it_is_cut():
    whole = screen(rows=6, cols=20, text=STREAM)
    for step in (1, 2, 3, 7):
        s = screen(rows=6, cols=20)
        for start in range(0, len(STREAM), step):
            s.feed(STREAM[start:start + step])
        assert state(s) == state(whole), step
        assert s.replies == whole.replies
    rng = random.Random(7)
    for _ in range(40):
        cuts = sorted(rng.sample(range(1, len(STREAM)), 12))
        s = screen(rows=6, cols=20)
        for a, b in zip([0] + cuts, cuts + [len(STREAM)]):
            s.feed(STREAM[a:b])
        assert state(s) == state(whole)


def test_an_unfinished_string_that_never_ends_is_not_kept_forever():
    s = screen()
    s.feed("\x1b]0;" + "x" * (vtscreen._CARRY_MAX + 10))
    assert len(s._carry) <= vtscreen._CARRY_MAX


# --------------------------------------------------------------------------- #
# wide and combining characters
# --------------------------------------------------------------------------- #
def test_character_widths():
    assert char_width("a") == 1 and char_width("é") == 1
    assert char_width("漢") == 2 and char_width("😀") == 2
    assert char_width("́") == 0 and char_width("‍") == 0


def test_wide_characters_take_two_cells():
    s = screen(rows=2, cols=5, text="a漢b")
    assert s.lines[0].chars[:4] == ["a", "漢", "", "b"]
    assert s.cursor == (0, 4)
    assert s.display()[0] == "a漢b"


def test_a_wide_character_at_the_last_column_wraps():
    s = screen(rows=2, cols=4, text="abc漢")
    assert s.display() == ["abc", "漢"]


def test_overwriting_half_a_wide_character_blanks_the_other_half():
    s = screen(rows=1, cols=6, text="漢字\x1b[2GX")
    assert s.lines[0].chars[:4] == [" ", "X", "字", ""]
    s.feed("\x1b[3GY")
    assert s.lines[0].chars[:4] == [" ", "X", "Y", " "]


def test_combining_marks_join_the_character_before():
    s = screen(text="éx")
    assert s.lines[0].chars[:2] == ["é", "x"]
    assert s.cursor == (0, 2)


# A cell with a character and the marks joined to it: emoji with VS16, a Thai
# vowel, Hebrew points.  char_width() of the whole cell raised TypeError.
MARKED = ["\u26a0\ufe0f", "\u2764\ufe0f", "\u0e01\u0e31", "\u05e9\u05b8", "e\u0301"]


@pytest.mark.parametrize("cell", MARKED)
def test_a_cell_with_marks_at_the_new_edge_survives_a_resize(cell):
    s = screen(rows=2, cols=8, text="abcd" + cell + "xyz")
    assert s.lines[0].chars[4] == cell
    s.resize(2, 5)
    assert s.cols == 5 and all(len(line.chars) == 5 for line in s.lines)
    assert s.lines[0].chars == ["a", "b", "c", "d", cell]
    s.resize(3, 7)
    assert (s.rows, s.cols) == (3, 7) and all(len(line.chars) == 7 for line in s.lines)


def test_a_wide_character_with_a_joiner_cut_in_half_goes():
    s = screen(rows=1, cols=8, text="abcd\U0001f469\u200d\U0001f4bb")  # a ZWJ sequence
    assert s.lines[0].chars[4:8] == ["\U0001f469\u200d", "", "\U0001f4bb", ""]
    s.resize(1, 5)
    assert s.lines[0].chars == ["a", "b", "c", "d", " "]


@pytest.mark.parametrize("cell", MARKED)
def test_inserted_cells_push_a_cell_with_marks_to_the_edge(cell):
    s = screen(rows=1, cols=6, text="abcd" + cell)
    s.feed("\x1b[1G\x1b[@")  # ICH: the cell moves to the last column
    assert s.lines[0].chars == [" ", "a", "b", "c", "d", cell]
    s.feed("\x1b[1G\x1b[4hX")  # insert mode: over the edge it goes
    assert s.lines[0].chars == ["X", " ", "a", "b", "c", "d"]


def test_a_resize_that_fails_changes_nothing(monkeypatch):
    """Everything is worked out before anything changes: a failure half-way
    left rows cut to the new width in a screen of the old one, and every
    repaint then read cells that were not there."""
    s = screen(rows=3, cols=8, text="abcdefgh\r\n1234Z678\r\nxyz")
    before = state(s)
    real = vtscreen.char_width

    def fails_on_z(ch):
        if ch == "Z":
            raise RuntimeError("no width")
        return real(ch)

    monkeypatch.setattr(vtscreen, "char_width", fails_on_z)
    with pytest.raises(RuntimeError):
        s.resize(2, 5)
    assert (s.rows, s.cols) == (3, 8) and state(s) == before
    assert all(len(line.chars) == len(line.attrs) == 8 for line in s.lines)


# --------------------------------------------------------------------------- #
# size, seeding
# --------------------------------------------------------------------------- #
def test_a_narrower_screen_cuts_rows_and_a_wider_one_pads():
    s = screen(rows=2, cols=6, text="abcdef\r\n漢字漢")
    s.resize(2, 5)
    assert s.display() == ["abcde", "漢字"]  # the half character at the edge goes
    assert s.cursor == (1, 4)
    s.resize(2, 8)
    assert s.display() == ["abcde", "漢字"]
    s.feed("\x1b[1;8HZ")
    assert s.display()[0] == "abcde  Z"


def test_a_shorter_screen_drops_blank_rows_first_then_scrolls():
    s = screen(rows=5, cols=4, text="a\r\nb\r\nc")
    s.resize(3, 4)
    assert s.display() == ["a", "b", "c"]
    assert s.take_scrolled_off() == []
    s.resize(2, 4)
    assert s.display() == ["b", "c"]
    assert [line.text() for line in s.take_scrolled_off()] == ["a"]
    assert s.cursor == (1, 1)
    s.resize(4, 4)
    assert s.display() == ["b", "c", "", ""]


def test_resizing_resets_the_scroll_region_and_clamps_the_cursor():
    s = screen(rows=6, cols=10, text="\x1b[2;4r\x1b[6;10H")
    s.resize(3, 5)
    assert (s.top, s.bottom) == (0, 2) and s.cursor == (2, 4)


def test_seeding_from_the_scrollback():
    s = Screen(3, 5)
    s.seed([[("ab", PLAIN), ("cdefg", (1, DEFAULT, BOLD))], [("漢x", PLAIN)]], (1, 3))
    assert s.display() == ["abcde", "漢x", ""]
    assert s.lines[0].attrs[2] == (1, DEFAULT, BOLD)
    assert [line.seed for line in s.lines] == [0, 1, None]
    assert s.cursor == (1, 3)
    s.feed("y")
    assert s.display()[1] == "漢xy"


# --------------------------------------------------------------------------- #
# what the device's own programs write
# --------------------------------------------------------------------------- #
def top_frame(n, rows=6, cols=40):
    """One refresh of toybox top: home + erase below, header lines ended
    with CR LF, the column header in reverse video, process lines joined by
    LF and each ended with a CR (the last one leaves the cursor there)."""
    header = [f"Tasks: {100 + n} total", "Mem: 3.6G total", f"{400 + n}%cpu"]
    out = "\x1b[H\x1b[J" + "".join(line + "\r\n" for line in header)
    out += "\x1b[7m" + "  PID USER".ljust(cols) + "\x1b[0m\r\n"
    procs = [f"{1000 + n + i:5d} shell  top" for i in range(rows - 4)]
    out += "\n".join(("\x1b[1m" if i == 0 else "") + p + ("\x1b[m" if i == 0 else "") + "\r"
                     for i, p in enumerate(procs))
    return out


def test_toybox_top_redraws_in_place():
    s = screen(rows=6, cols=40, text="PD2318:/ $ top\r\n")
    s.feed("\x1b[?25l" + top_frame(1))
    first = s.display()
    assert first[0] == "Tasks: 101 total" and first[5].startswith(" 1002")
    s.feed(top_frame(2))
    assert s.display()[0] == "Tasks: 102 total"
    assert s.display()[4].startswith(" 1002") and s.display()[5].startswith(" 1003")
    assert s.take_scrolled_off() == []  # nothing scrolls: frame over frame
    assert s.lines[3].attrs[0][2] == REVERSE and not s.cursor_visible
    # q: tty_reset() puts the cursor on the last row, the shell its prompt
    s.feed("\x1b[?25h\x1b[0m\x1b[999H\x1b[KPD2318:/ $ ")
    assert s.cursor == (5, 11) and s.cursor_visible
    assert s.display()[5] == "PD2318:/ $"
    assert s.wiped


def test_toybox_vi_uses_the_alternate_screen():
    s = screen(rows=5, cols=20, text="PD2318:/ $ vi f\r\n")
    s.feed("\x1b[?1049h\x1b[2J\x1b[H")
    s.feed("hello\x1b[2;1H\x1b[2m~\x1b[m\x1b[3;1H\x1b[2m~\x1b[m\x1b[5;0H\x1b[2K\x1b[1m-- INSERT --\x1b[m")
    assert s.display() == ["hello", "~", "~", "", "-- INSERT --"]
    # lines inserted above the text, inside a region that spares the status line
    s.feed("\x1b[1;4r\x1b[1;6H!\x1b[2L\x1b[41m\x1b[37m\x1b[K\x1b[1mE\x1b[0m")
    assert s.display() == ["E", "", "hello!", "~", "-- INSERT --"]
    assert s.lines[0].attrs[5] == (DEFAULT, 1, 0)  # the red error line, erased in red
    s.feed("\x1b[1;1H\x1b[2M")
    assert s.display() == ["hello!", "~", "", "", "-- INSERT --"]
    s.feed("\x1b[?1049l")
    assert s.display() == ["PD2318:/ $ vi f", "", "", "", ""]
    assert s.cursor == (1, 0)


# --------------------------------------------------------------------------- #
# keys, and what the console hands over
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("name, mods, app, sent", [
    ("up", {}, False, "\x1b[A"), ("up", {}, True, "\x1bOA"),
    ("left", {"shift": True}, False, "\x1b[1;2D"), ("right", {"ctrl": True}, True, "\x1b[1;5C"),
    ("home", {}, False, "\x1b[H"), ("end", {}, True, "\x1bOF"),
    ("insert", {}, False, "\x1b[2~"), ("delete", {}, False, "\x1b[3~"),
    ("pageup", {}, False, "\x1b[5~"), ("pagedown", {"alt": True}, False, "\x1b[6;3~"),
    ("f1", {}, False, "\x1bOP"), ("f4", {"shift": True}, False, "\x1b[1;2S"),
    ("f5", {}, False, "\x1b[15~"), ("f12", {}, False, "\x1b[24~"),
    ("enter", {}, False, "\n"), ("enter", {"alt": True}, False, "\x1b\n"),
    ("backspace", {}, False, "\x7f"), ("backspace", {"ctrl": True}, False, "\x08"),
    ("tab", {}, False, "\t"), ("tab", {"shift": True}, False, "\x1b[Z"),
    ("escape", {}, False, "\x1b"), ("nothing", {}, False, ""),
])
def test_key_sequences(name, mods, app, sent):
    assert vtscreen.key_sequence(name, app_cursor=app, **mods) == sent


@pytest.mark.parametrize("key, sent", [
    ("a", "\x01"), ("C", "\x03"), ("z", "\x1a"), ("[", "\x1b"), ("\\", "\x1c"),
    ("]", "\x1d"), ("^", "\x1e"), ("_", "\x1f"), ("@", "\x00"), (" ", "\x00"),
    ("?", "\x7f"), ("2", "\x00"), ("6", "\x1e"), ("8", "\x7f"), ("1", ""), ("ab", ""),
])
def test_control_characters(key, sent):
    assert vtscreen.control_character(key) == sent


def test_colours_and_a_saved_cursor_from_before_the_screen():
    s = screen(rows=3, cols=6)
    s.use_attr(2, DEFAULT, BOLD)
    s.save_cursor_at(9, 2)
    s.feed("x\x1b[3;3H\x1b8y")
    assert s.lines[0].attrs[0] == (2, DEFAULT, BOLD)
    assert s.cursor == (2, 3) and s.display()[2] == "  y"


def test_leaving_an_alternate_screen_a_program_never_left():
    s = screen(rows=2, cols=4, text="ab\x1b[?1049hvi")
    s.leave_alternate()
    assert not s.alt_active and s.display()[0] == "ab" and s.cursor == (0, 2)
    t = screen(rows=2, cols=4, text="ab\x1b[?47hvi")  # 47 saved nothing: the cursor stays
    t.leave_alternate()
    assert t.display()[0] == "ab" and t.cursor == (0, 3)
