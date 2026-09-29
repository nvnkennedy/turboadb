"""Running commands as root on any rooted build, what `adb remount` really
did, and port rules on a port adb picked itself."""
import pytest

from turboadb import ADBConfig, ADBHandler
from turboadb.exceptions import ADBCommandError


def _dev(serial="S1"):
    return ADBHandler(ADBConfig(serial=serial))


def _commands(fake_adb):
    return [c[-1] for c in fake_adb.calls if "shell" in c]


# --------------------------------------------------------------------------- #
# shell --su
# --------------------------------------------------------------------------- #
def test_aosp_su_on_userdebug_builds_takes_a_user_not_minus_c(fake_adb):
    # su -c: "su: invalid uid/gid '-c'"; su 0 sh -c: root
    fake_adb.add("@@su=", stdout="@@su=0\n")
    res = _dev().shell("cat /data/misc/file", su=True)
    assert res.ok
    probe, command = _commands(fake_adb)
    assert "su -c id" in probe and "su 0 sh -c id" in probe
    assert "su 0 sh -c 'cat /data/misc/file'" in command
    assert "su -c 'cat" not in command


def test_magisk_su_keeps_minus_c(fake_adb):
    fake_adb.add("@@su=", stdout="@@su=c\n")
    _dev().shell("id", su=True)
    assert "su -c id" in _commands(fake_adb)[-1]


def test_adbd_running_as_root_needs_no_su_at_all(fake_adb):
    fake_adb.add("@@su=", stdout="@@su=none\n")  # an eng build without su
    _dev().shell("ls /data", su=True)
    command = _commands(fake_adb)[-1]
    # checked on the device each time: adb root / unroot change it
    assert command.startswith('case "$(id)" in "uid=0("*) sh -c \'ls /data\';;')
    assert command.endswith("*) su -c 'ls /data';; esac")  # else the device's own error


def test_the_su_form_is_asked_once_per_device(fake_adb):
    fake_adb.add("@@su=", stdout="@@su=0\n")
    dev = _dev()
    dev.shell_many(["echo a", "echo b", "echo c"], su=True)
    assert sum("@@su=" in c for c in _commands(fake_adb)) == 1
    dev._serial = "S2"  # another device behind the same handler asks again
    dev.shell("echo d", su=True)
    assert sum("@@su=" in c for c in _commands(fake_adb)) == 2


def test_an_unanswered_su_question_is_asked_again(fake_adb):
    dev = _dev()  # the fake device prints nothing: no answer
    dev.shell("echo a", su=True)
    dev.shell("echo b", su=True)
    commands = _commands(fake_adb)
    assert sum("@@su=" in c for c in commands) == 2
    assert "su -c 'echo b'" in commands[-1]


def test_without_su_the_command_is_sent_as_it_is(fake_adb):
    _dev().shell("getprop ro.build.type")
    assert _commands(fake_adb) == ["getprop ro.build.type"]


# --------------------------------------------------------------------------- #
# remount
# --------------------------------------------------------------------------- #
def test_remount_says_when_it_still_needs_a_reboot(fake_adb):
    fake_adb.add("remount", stdout="Disabling verity for /system\n"
                                   "Now reboot your device for settings to take effect\n")
    dev = _dev()
    logged = []
    dev.set_log_callback(logged.append)
    text = dev.remount()
    assert text.endswith("/system stays read-only until then.")
    assert any(line.startswith("[WARNING] adb remount: Reboot the device") for line in logged)


def test_remount_success_and_refusal_are_judged_one_way(fake_adb):
    fake_adb.add("remount", stdout="Using overlayfs for /system\nremount succeeded")
    assert _dev().remount() == "Using overlayfs for /system\nremount succeeded"
    fake_adb.rules.clear()
    fake_adb.add("remount", returncode=1, stderr="remount failed: not running as root")
    with pytest.raises(ADBCommandError, match="not running as root"):
        _dev().remount()
    assert _dev().remount(safe=True).success is False


# --------------------------------------------------------------------------- #
# forward / reverse tcp:0
# --------------------------------------------------------------------------- #
def test_forward_tcp0_keeps_the_port_adb_picked(fake_adb):
    fake_adb.add("forward tcp:0", stdout="40123\n")
    dev = _dev()
    handle = dev.forward("tcp:0", "localabstract:chrome_devtools_remote")
    assert handle.local == "tcp:40123"
    assert "tcp:40123 ->" in repr(handle)
    assert handle.close() is True
    assert fake_adb.argv_after_adb()[-3:] == ["forward", "--remove", "tcp:40123"]


def test_reverse_tcp0_keeps_the_devices_port(fake_adb):
    fake_adb.add("reverse tcp:0", stdout="38001\n")
    handle = _dev().reverse("tcp:0", "tcp:8000")
    assert (handle.remote, handle.local) == ("tcp:38001", "tcp:8000")
    handle.close()
    assert fake_adb.argv_after_adb()[-3:] == ["reverse", "--remove", "tcp:38001"]


def test_a_fixed_port_or_an_unexpected_answer_keeps_the_spec(fake_adb):
    fake_adb.add("forward tcp:9222", stdout="40123\n")  # never taken for a fixed port
    assert _dev().forward("tcp:9222", "tcp:9222").local == "tcp:9222"
    fake_adb.add("forward tcp:0", stdout="")  # an adb that does not print it
    assert _dev().forward("tcp:0", "tcp:1").local == "tcp:0"
