"""Terminal output made plain, and one way to print a size everywhere."""
from turboadb import remotefs
from turboadb.results import TransferResult, human_bytes, strip_ansi

ESC = "\x1b"


def test_charset_choices_leave_nothing_behind():
    # terminfo's sgr0 is ESC ( B ESC [ m: top, vi and the like print it all the time
    assert strip_ansi(f"{ESC}(B{ESC}[mhello{ESC})0x") == "hellox"
    assert strip_ansi(f"{ESC}$)Aw") == "w"


def test_two_byte_escapes_leave_nothing_behind():
    assert strip_ansi(f"{ESC}7saved{ESC}8") == "saved"  # save / restore the cursor
    assert strip_ansi(f"{ESC}[?1h{ESC}=keypad{ESC}>") == "keypad"  # keypad modes
    assert strip_ansi(f"{ESC}creset") == "reset"
    assert strip_ansi(f"{ESC}Mup") == "up"


def test_osc_and_csi_still_come_first():
    assert strip_ansi(f"{ESC}]0;my title\x07hello") == "hello"
    assert strip_ansi(f"{ESC}]8;;http://x{ESC}\\link{ESC}]8;;{ESC}\\") == "link"
    assert strip_ansi(f"{ESC}[1;32mok{ESC}[0m\r\n") == "ok\n"
    assert strip_ansi("plain [text] (with brackets)") == "plain [text] (with brackets)"


def test_one_size_format_for_every_caller():
    assert human_bytes(None) == "—"
    assert human_bytes(0) == "0 B" and human_bytes(-5) == "0 B"
    assert human_bytes(500) == "500 B" and human_bytes(1023.9) == "1023 B"
    assert human_bytes(1536) == "1.5 KB"
    assert human_bytes(5 * 1024 ** 3) == "5.0 GB"
    assert human_bytes(3 * 1024 ** 5) == "3072.0 TB"
    # the Files tab's listing and the Transfers panel use the very same function
    from turboadb.gui import transfer_log

    assert remotefs._human_size is human_bytes and transfer_log.human_bytes is human_bytes


def test_transfer_results_print_sizes_as_the_gui_does():
    tr = TransferResult("a", "b", "pull", 12 * 1024 ** 2, 0.25)
    assert tr.human_size == "12.0 MB" and tr.human_speed == "48.0 MB/s"
    assert "(12.0 MB in 0.25s, 48.0 MB/s)" in str(tr)
    assert TransferResult("a", "b", "push", 12, 0.0).human_speed == "0 B/s"
