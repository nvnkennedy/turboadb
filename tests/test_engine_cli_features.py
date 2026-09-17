"""Engine methods added for the complete CLI, against the fake adb."""
import io
import os
import shlex
import sys
import threading

import pytest

import turboadb
import turboadb.core as core
from turboadb import ADBConfig, ADBHandler, remotefs
from turboadb.exceptions import (
    ADBCommandError,
    ADBConnectionError,
    ADBError,
    ADBNotConnectedError,
    ADBTimeoutError,
)


def handler():
    return ADBHandler(ADBConfig(serial="x"))


def joined(fake_adb):
    return [" ".join(c[1:]) for c in fake_adb.calls]


def device_words(fake_adb, index=-1):
    """What the DEVICE shell receives: adb joins the words after `shell` with
    spaces and runs them through `sh -c`, which shlex.split imitates."""
    argv = fake_adb.argv_after_adb(index)
    return shlex.split(" ".join(argv[argv.index("shell") + 1:]))


def test_open_url_accepts_shortcut_names(fake_adb):
    assert handler().open_url(" YouTube ") is True
    assert any("https://www.youtube.com" in arg for arg in fake_adb.argv_after_adb())
    handler().open_url("play-store")
    assert any("https://play.google.com/store/apps" in arg for arg in fake_adb.argv_after_adb())


def test_battery_status_parses_numbers_and_booleans(fake_adb):
    fake_adb.add("dumpsys battery", stdout=(
        "Current Battery Service state:\n  AC powered: false\n  USB powered: true\n"
        "  level: 87\n  temperature: 301\n  technology: Li-ion\n"))
    info = handler().battery_status()
    assert info["level"] == 87 and info["temperature"] == 301
    assert info["USB_powered"] is True and info["AC_powered"] is False
    assert info["technology"] == "Li-ion"


def test_build_properties_is_a_dict_of_strings(fake_adb):
    fake_adb.add("getprop", stdout="[ro.product.model]: [Pixel 8]\n[ro.build.version.release]: [14]\n")
    props = handler().build_properties()
    assert props["ro.product.model"] == "Pixel 8"
    assert all(isinstance(v, str) for v in props.values())


def test_reverse_rules_list_and_remove(fake_adb):
    fake_adb.add("reverse --list", stdout="x tcp:3000 tcp:3000\n")
    h = handler()
    assert h.list_reverses() == ["x tcp:3000 tcp:3000"]
    assert h.remove_reverse("tcp:3000") is True
    assert fake_adb.argv_after_adb()[-3:] == ["reverse", "--remove", "tcp:3000"]


def test_wait_for_boot(fake_adb):
    fake_adb.add("sys.boot_completed", stdout="1\n")
    assert handler().wait_for_boot(30) is True
    calls = joined(fake_adb)
    assert any("wait-for-disconnect" in c for c in calls)
    assert any("wait-for-device" in c for c in calls)


def test_wait_for_boot_times_out(fake_adb):
    with pytest.raises(ADBTimeoutError):
        handler().wait_for_boot(0)


def test_stat_path_and_chmod(fake_adb):
    fake_adb.add("stat -L", stdout="1234 644 regular file\n/data/real.ini\n")
    info = handler().stat_path("/data/link.ini")
    assert info == {"path": "/data/link.ini", "real_path": "/data/real.ini", "size": 1234,
                    "mode": "644", "type": "regular file"}
    with pytest.raises((ValueError, ADBError)):
        handler().chmod("/data/real.ini", "rwx")
    assert handler().chmod("/data/real.ini", "600") is True
    assert "chmod 600 -- /data/real.ini" in joined(fake_adb)[-1]


def test_stat_path_missing_raises(fake_adb):
    fake_adb.add("stat -L", stdout="", stderr="stat: /nope: No such file or directory", returncode=1)
    with pytest.raises(ADBError, match="/nope"):
        handler().stat_path("/nope")


def test_remove_refuses_folders_without_recursive_and_root(fake_adb):
    fake_adb.add("for f in", stdout="d- /sdcard/logs\n")
    with pytest.raises(ADBError, match="is a folder"):
        handler().remove("/sdcard/logs")
    assert not any("rm -rf" in c for c in joined(fake_adb))
    assert handler().remove(["/sdcard/logs"], recursive=True) == ["/sdcard/logs"]
    assert any("rm -rf /sdcard/logs" in c for c in joined(fake_adb))


def test_remove_refuses_root(fake_adb):
    fake_adb.add("for f in", stdout="d- /\n")
    with pytest.raises(ADBError, match="refusing to delete /"):
        handler().remove("/", recursive=True)
    assert not any("rm -rf" in c for c in joined(fake_adb))


def test_make_dir_and_touch(fake_adb):
    h = handler()
    assert h.make_dir("/sdcard/a/b/") == "/sdcard/a/b"
    assert h.touch("/sdcard/a/b/c.txt") == "/sdcard/a/b/c.txt"
    calls = joined(fake_adb)
    assert any("mkdir -p" in c and "/sdcard/a/b" in c for c in calls)


def test_move_into_an_existing_folder(fake_adb):
    fake_adb.add("for f in", stdout="f- \nd- /sdcard/Movies\n")
    assert handler().move("/sdcard/clip.mp4", "/sdcard/Movies") == "/sdcard/Movies/clip.mp4"
    assert "/sdcard/Movies/clip.mp4" in joined(fake_adb)[-1]


def test_move_missing_source_changes_nothing(fake_adb):
    fake_adb.add("for f in", stdout="n- \nd- /sdcard\n")
    with pytest.raises(ADBError, match="no such file"):
        handler().move("/sdcard/gone.txt", "/sdcard")
    assert len(fake_adb.calls) == 1  # only the probe ran


def test_copy_a_file_to_a_new_name(fake_adb):
    fake_adb.add("for f in", stdout="f- \nn- \n")
    assert handler().copy("/sdcard/a.txt", "/sdcard/b.txt") == "/sdcard/b.txt"
    assert "/sdcard/b.txt" in joined(fake_adb)[-1]


def test_file_command_failure_is_an_error(fake_adb):
    fake_adb.add("mkdir", stderr="mkdir: '/system/x': Read-only file system", returncode=1)
    with pytest.raises(ADBError, match="Read-only"):
        handler().make_dir("/system/x")


# --------------------------------------------------------------------------- #
# Device paths: a space belongs to the name; the shell may be colourising
# --------------------------------------------------------------------------- #
def test_remote_paths_keep_their_spaces():
    # Only line breaks are trimmed — "dup " and "dup" are two different files.
    assert remotefs._normalize_remote_path("/sdcard/dup ") == "/sdcard/dup "
    assert remotefs._normalize_remote_path("/sdcard/dup \n") == "/sdcard/dup "
    assert remotefs._normalize_remote_path("/sdcard/a/b/\r\n") == "/sdcard/a/b"
    assert remotefs._normalize_remote_path("\r\n") == "/"
    assert remotefs._normalize_remote_path("") == "/"


def test_remove_deletes_the_name_with_the_trailing_space(fake_adb):
    fake_adb.add("for f in", stdout="f- \n")
    assert handler().remove("/sdcard/dup ") == ["/sdcard/dup "]
    assert device_words(fake_adb) == ["rm", "-rf", "/sdcard/dup "]


def test_output_lines_strips_shell_colours():
    assert remotefs._output_lines("\x1b[0;32mf-\x1b[0m /sdcard/a\n") == ["f- /sdcard/a"]


def test_probe_replies_survive_a_colourising_shell(fake_adb):
    # A device shell that colours its output made every probe reply look
    # "unexpected", so rm/mv/cp refused to run at all.
    fake_adb.add("for f in", stdout="\x1b[0;32mf-\x1b[0m /sdcard/a.txt\n")
    assert handler().remove("/sdcard/a.txt") == ["/sdcard/a.txt"]
    assert device_words(fake_adb) == ["rm", "-rf", "/sdcard/a.txt"]


def test_touch_fallback_never_truncates_an_existing_file():
    cmd = remotefs._touch_cmd("/sdcard/a b.txt")
    assert cmd.startswith("touch '/sdcard/a b.txt'")
    # the redirect is reached only after the existence test
    assert cmd.index("[ -e ") < cmd.index(": > ")
    words = shlex.split(cmd)
    assert words[:2] == ["touch", "/sdcard/a b.txt"] and words[-1] == "/sdcard/a b.txt"


# --------------------------------------------------------------------------- #
# ls parsing: locale dates, and honest uncertainty when the date is unknown
# --------------------------------------------------------------------------- #
def test_locale_dates_keep_the_whole_file_name():
    row, exact = remotefs._parse_ls_entry(
        "-rw-r--r-- 1 root root 1234 sept. 14  2026 note.txt"
    )
    assert row[0] == "note.txt" and exact
    assert row[4] == "sept. 14  2026"
    row, exact = remotefs._parse_ls_entry("-rw-r--r-- 1 root root 12 14 sept. 2026 note.txt")
    assert row[0] == "note.txt" and exact


def test_an_unreadable_date_marks_the_row_uncertain():
    parsed = remotefs._parse_ls_entry("-rw-r--r-- 1 root root 1234 ?? ?? note.txt")
    assert parsed is not None
    row, exact = parsed
    assert row[0] == "note.txt" and exact is False
    rows, uncertain, clean = remotefs._parse_ls_listing(
        "-rw-r--r-- 1 root root 1234 ?? ?? note.txt\n"
    )
    assert [r[0] for r in rows] == ["note.txt"]
    assert uncertain == {"note.txt"} and clean is False  # the caller retries with ls -lab


def test_link_type_probe_respects_the_character_budget(fake_adb):
    # 100 long symlink names: one hardcoded batch of 100 built a shell command
    # far past what the device accepts, so _chunks caps it by characters too.
    names = ["lnk%03d" % i + "x" * 600 for i in range(100)]
    fake_adb.add("ls -la", stdout="".join(
        "lrwxrwxrwx 1 root root 7 2026-01-01 00:00 %s -> /target\n" % n for n in names
    ))
    handler().list_dir("/sdcard")
    probes = [c for c in joined(fake_adb) if "if [ -d " in c]
    assert len(probes) >= 3
    assert max(len(c) for c in probes) < 26000


# --------------------------------------------------------------------------- #
# Lazy imports live inside the guarded closure, so safe mode never raises
# --------------------------------------------------------------------------- #
def test_a_failing_lazy_import_is_caught_by_safe_mode(fake_adb, monkeypatch):
    monkeypatch.setitem(sys.modules, "turboadb.remotefs", None)
    monkeypatch.delattr(turboadb, "remotefs", raising=False)
    res = handler().list_dir("/sdcard", safe=True)
    assert res.success is False and isinstance(res.error, ImportError)


# --------------------------------------------------------------------------- #
# Device queries fail loudly instead of returning blanks
# --------------------------------------------------------------------------- #
def test_getprop_and_battery_report_a_failed_call(fake_adb):
    fake_adb.add("getprop", stderr="error: device offline", returncode=1)
    fake_adb.add("dumpsys battery", stderr="error: device offline", returncode=1)
    h = handler()
    for call in (h.getprop, h.battery, h.battery_status, h.build_info, h.build_properties):
        with pytest.raises(ADBCommandError):
            call()


def test_build_info_is_the_printed_form_of_build_properties(fake_adb):
    fake_adb.add("getprop", stdout="[ro.product.model]: [Pixel 8]\n")
    h = handler()
    props, text = h.build_properties(), h.build_info()
    assert list(props) == [line.split()[0] for line in text.splitlines()]
    assert "Pixel 8" in text


def test_quick_identity_does_not_shift_when_a_property_is_missing(fake_adb):
    # The device has no ro.product.manufacturer: it prints an empty first line.
    fake_adb.add("getprop ro.product.manufacturer", stdout="\nV2318\n16\n36\nPD2323F\narm64-v8a\n")
    ident = handler().quick_identity()
    assert ident["manufacturer"] == "" and ident["model"] == "V2318"
    assert ident["sdk"] == "36" and ident["abi"] == "arm64-v8a"


# --------------------------------------------------------------------------- #
# Gestures and the display flag
# --------------------------------------------------------------------------- #
def test_gestures_refuse_a_guessed_screen_size(fake_adb):
    # Neither dumpsys nor wm size answers: a 1080x1920 default aimed every
    # gesture off-screen on a 1920x720 head unit and still reported success.
    h = handler()
    for call in (lambda: h.scroll("up"), h.tap_center):
        with pytest.raises(ADBError, match="display size"):
            call()
    assert not any("input" in c for c in joined(fake_adb))


def test_display_flag_validates_and_quotes():
    h = handler()
    assert h._display_flag(None) == []
    assert h._display_flag("2") == ["-d", "2"] == h._display_flag(2)
    with pytest.raises(ValueError):
        h._display_flag("2; reboot")


def test_screenshot_refuses_a_non_numeric_display(fake_adb, tmp_path):
    with pytest.raises(ValueError):
        handler().screenshot(str(tmp_path / "s.png"), display_id="1; reboot")
    assert not fake_adb.calls


# --------------------------------------------------------------------------- #
# `am` exits 0 while refusing, so its OUTPUT decides
# --------------------------------------------------------------------------- #
def test_open_app_accepts_the_brought_to_front_warning(fake_adb):
    # AOSP prints this on SUCCESS; treating "not started" as a failure sent
    # open_camera off hunting through every installed package.
    fake_adb.add("am start", stdout=(
        "Warning: Activity not started, its current task has been brought to the front\n"))
    assert handler().open_camera() is True
    assert not any("pm list packages" in c for c in joined(fake_adb))


def test_open_settings_reports_an_unresolved_intent(fake_adb):
    fake_adb.add(
        "android.settings.SETTINGS",
        stdout="Error: Activity not started, unable to resolve Intent",
    )
    assert handler().open_settings() is False


def test_start_activity_raises_when_the_class_does_not_exist(fake_adb):
    fake_adb.add("am start", stdout="Error: Activity class {com.x/.Nope} does not exist.")
    with pytest.raises(ADBCommandError):
        handler().start_activity("com.x/.Nope")


def test_stop_app_raises_when_am_refuses(fake_adb):
    fake_adb.add("am force-stop", stdout="Error: Permission Denial: not allowed")
    with pytest.raises(ADBCommandError):
        handler().stop_app("com.x")


def test_toggle_attempts_are_quoted_for_the_device_shell(fake_adb):
    h = handler()
    assert h._try_toggles("t", [(["svc", "wifi", "enable; reboot"], "ok")], "blocked") == "ok"
    assert device_words(fake_adb) == ["svc", "wifi", "enable; reboot"]


def test_airplane_fallback_words_reach_the_device_intact(fake_adb):
    fake_adb.add("cmd connectivity airplane-mode", stdout="unknown command", returncode=1)
    handler().set_airplane(True)
    assert device_words(fake_adb) == [
        "am", "broadcast", "-a", "android.intent.action.AIRPLANE_MODE", "--ez", "state", "true",
    ]
    assert device_words(fake_adb, -2) == ["settings", "put", "global", "airplane_mode_on", "1"]


# --------------------------------------------------------------------------- #
# A target adb does not know about is ADBNotConnectedError
# --------------------------------------------------------------------------- #
def test_wait_for_device_reports_an_unknown_target(fake_adb):
    fake_adb.add("wait-for-device", stderr="error: device '10.0.0.5:5555' not found", returncode=1)
    with pytest.raises(ADBNotConnectedError, match="not connected"):
        handler().wait_for_device(5)


def test_wait_for_device_keeps_other_failures_as_connection_errors(fake_adb):
    fake_adb.add("wait-for-device", stderr="error: protocol fault", returncode=1)
    with pytest.raises(ADBConnectionError):
        handler().wait_for_device(5)


def test_public_host_port_helpers_are_exported():
    for name in ("validate_port", "format_host_port", "parse_host_port"):
        assert name in turboadb.__all__
        assert callable(getattr(turboadb, name))


# --------------------------------------------------------------------------- #
# Saving to a local file: ~ is expanded and the folder is created
# --------------------------------------------------------------------------- #
class _FinishedProc:
    """A screenrecord child that has already exited cleanly."""

    def __init__(self, *_a, **_k):
        self.stdout = io.BytesIO(b"")

    def poll(self):
        return 0

    def wait(self, timeout=None):
        return 0

    def terminate(self):
        pass

    def kill(self):
        pass


def _home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))


def test_logcat_grep_filters_the_stream_and_expands_the_save_path(fake_adb, monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    h = handler()
    monkeypatch.setattr(h, "iter_lines", lambda *a, **k: iter(["boot ok", "FATAL boom", "quiet"]))
    seen = []
    res = h.logcat(grep="fatal", dump=True, save_to="~/logs/boot.log", on_line=seen.append)
    saved = tmp_path / "logs" / "boot.log"
    assert seen == ["FATAL boom"] and res.lines == 1
    assert saved.read_text().splitlines() == ["FATAL boom"]
    assert os.path.normpath(res.saved_to) == os.path.normpath(str(saved))


def test_logcat_crashes_picks_the_crash_buffers_and_error_level(fake_adb, monkeypatch):
    h = handler()
    seen = {}

    def fake_iter(args, **_kw):
        seen["args"] = list(args)
        return iter(())

    monkeypatch.setattr(h, "iter_lines", fake_iter)
    h.logcat(crashes=True, dump=True)
    assert seen["args"].count("-b") == 3 and "crash" in seen["args"] and "*:E" in seen["args"]
    # an explicit choice always wins
    h.logcat(crashes=True, dump=True, buffers=["main"], priority="W")
    assert seen["args"].count("-b") == 1 and "*:W" in seen["args"]


def test_screen_record_expands_the_save_path(fake_adb, monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    h = handler()
    monkeypatch.setattr(core.subprocess, "Popen", _FinishedProc)

    def fake_transfer(direction, _remote, local, *_rest):
        assert direction == "pull"
        with open(local, "wb") as fh:
            fh.write(b"mp4")
        return "ok"

    monkeypatch.setattr(h, "_transfer", fake_transfer)
    out = h.screen_record("~/clips/drive.mp4", time_limit=1)
    assert os.path.normpath(out) == os.path.normpath(str(tmp_path / "clips" / "drive.mp4"))
    assert os.path.isfile(out)


# --------------------------------------------------------------------------- #
# edit_file — the device half of `turboadb edit`
# --------------------------------------------------------------------------- #
def _edit_handler(fake_adb, monkeypatch, *, ftype="regular file", body=b"old\n"):
    fake_adb.add("stat -L", stdout="4 640 %s\n/data/app.ini\n" % ftype)
    h = handler()
    pushed = []

    def fake_transfer(direction, a, b, *_rest):
        if direction == "pull":
            with open(b, "wb") as fh:
                fh.write(body)
        else:
            with open(a, "rb") as fh:
                pushed.append((fh.read(), b))
        return "ok"

    monkeypatch.setattr(h, "_transfer", fake_transfer)
    return h, pushed


def test_edit_file_pushes_back_only_what_changed(fake_adb, monkeypatch):
    h, pushed = _edit_handler(fake_adb, monkeypatch)
    seen = {}

    def edit(path):
        seen["path"] = path
        with open(path, "wb") as fh:
            fh.write(b"new\n")
        return 0

    out = h.edit_file("/data/link.ini", edit, editor="vi")
    assert out == {"changed": True, "path": "/data/app.ini", "editor_exit": 0}
    assert pushed == [(b"new\n", "/data/app.ini")]
    assert device_words(fake_adb) == ["chmod", "640", "--", "/data/app.ini"]  # push resets the mode
    assert not os.path.exists(seen["path"])  # the temporary copy is always removed


def test_edit_file_leaves_an_unchanged_file_alone(fake_adb, monkeypatch):
    h, pushed = _edit_handler(fake_adb, monkeypatch)
    out = h.edit_file("/data/app.ini", lambda path: 0)
    assert out == {"changed": False, "path": "/data/app.ini", "editor_exit": 0}
    assert pushed == [] and not any("chmod" in c for c in joined(fake_adb))


def test_edit_file_keeps_the_device_file_when_the_editor_fails(fake_adb, monkeypatch):
    h, pushed = _edit_handler(fake_adb, monkeypatch)

    def edit(path):
        with open(path, "wb") as fh:
            fh.write(b"half written")
        return 130

    out = h.edit_file("/data/app.ini", edit)
    assert out == {"changed": False, "path": "/data/app.ini", "editor_exit": 130}
    assert pushed == []


def test_edit_file_refuses_anything_but_a_regular_file(fake_adb, monkeypatch):
    h, _pushed = _edit_handler(fake_adb, monkeypatch, ftype="directory")
    with pytest.raises(ADBError, match="not a regular file"):
        h.edit_file("/data", lambda path: 0)


# --------------------------------------------------------------------------- #
# screen_record_continuous — parts past screenrecord's 3-minute cap
# --------------------------------------------------------------------------- #
def test_screen_record_continuous_names_and_reports_every_part(fake_adb, tmp_path, monkeypatch):
    h = handler()
    stop = threading.Event()
    made = []

    def fake_record(path, **_kw):
        made.append(path)
        if len(made) == 3:
            stop.set()
        return path

    monkeypatch.setattr(h, "screen_record", fake_record)
    reported = []
    paths = h.screen_record_continuous(
        str(tmp_path / "drive.mp4"), stop_event=stop, on_part=reported.append
    )
    assert [os.path.basename(p) for p in paths] == [
        "drive.mp4", "drive-part02.mp4", "drive-part03.mp4",
    ]
    assert reported == paths == made


def test_screen_record_continuous_records_one_part_without_a_stop_event(
    fake_adb, tmp_path, monkeypatch
):
    h = handler()
    monkeypatch.setattr(h, "screen_record", lambda path, **_kw: path)
    assert h.screen_record_continuous(str(tmp_path / "clip.mp4")) == [str(tmp_path / "clip.mp4")]


def test_screen_record_continuous_raises_when_the_first_part_fails(fake_adb, tmp_path, monkeypatch):
    h = handler()

    def boom(path, **_kw):
        raise ADBError("screenrecord is blocked on this build")

    monkeypatch.setattr(h, "screen_record", boom)
    with pytest.raises(ADBError, match="blocked"):
        h.screen_record_continuous(str(tmp_path / "clip.mp4"), stop_event=threading.Event())


def test_screen_record_continuous_reports_earlier_parts_before_re_raising(
    fake_adb, tmp_path, monkeypatch
):
    h = handler()
    made = []

    def fake_record(path, **_kw):
        if len(made) == 2:
            raise ADBError("the device went away")
        made.append(path)
        return path

    monkeypatch.setattr(h, "screen_record", fake_record)
    reported = []
    with pytest.raises(ADBError, match="went away"):
        h.screen_record_continuous(
            str(tmp_path / "drive.mp4"), stop_event=threading.Event(), on_part=reported.append
        )
    assert [os.path.basename(p) for p in reported] == ["drive.mp4", "drive-part02.mp4"]
