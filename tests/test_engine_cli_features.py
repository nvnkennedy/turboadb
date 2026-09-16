"""Engine methods added for the complete CLI, against the fake adb."""
import pytest

from turboadb import ADBConfig, ADBHandler
from turboadb.exceptions import ADBError, ADBTimeoutError


def handler():
    return ADBHandler(ADBConfig(serial="x"))


def joined(fake_adb):
    return [" ".join(c[1:]) for c in fake_adb.calls]


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
