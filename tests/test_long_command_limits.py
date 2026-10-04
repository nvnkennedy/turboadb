"""An install, a device copy and a delete run as long as their data takes:
each has a generous time limit of its own, ``command_timeout`` (the CLI's
``--timeout``) can only raise it, and no ``command_timeout`` means no limit.
An install's limit grows with the APK, so a large one over a slow link is not
cut off."""
import shlex

import pytest

import turboadb.core as core
from turboadb import ADBConfig, ADBHandler

_MB = 1024 * 1024


@pytest.fixture
def limits(fake_adb, monkeypatch):
    """``limits(word)``: the time limit the adb command with *word* ran under."""
    seen = []

    def run(cmd, **kwargs):
        seen.append((" ".join(cmd[1:]), kwargs.get("timeout")))
        return fake_adb.run(cmd, **kwargs)

    def answer(argv):
        command = " ".join(argv)
        if "install" in command:
            return 0, b"Success\n", b""
        if "for f in " in command:  # the path probe: /sdcard/maps is a folder, the rest free
            paths = shlex.split(command.split("for f in ", 1)[1].split("; do ", 1)[0])
            kinds = {"/sdcard": "d- /sdcard", "/sdcard/maps": "d- /sdcard/maps"}
            return 0, "".join(kinds.get(p, "n- ") + "\n" for p in paths).encode(), b""
        return 0, b"", b""

    monkeypatch.setattr(core.subprocess, "run", run)
    monkeypatch.setattr(fake_adb, "_answer", answer)

    def limit(word):
        found = [timeout for command, timeout in seen if word in command]
        assert len(found) == 1, seen
        return found[0]

    return limit


def _apk(tmp_path, monkeypatch, megabytes, name="app.apk"):
    """An APK that counts as *megabytes* large without filling the disk."""
    path = tmp_path / name
    path.write_bytes(b"apk")
    real = core.os.path.getsize
    monkeypatch.setattr(core.os.path, "getsize",
                        lambda p: megabytes * _MB if str(p) == str(path) else real(p))
    return str(path)


def _handler(**config):
    return ADBHandler(ADBConfig(serial="x", **config))


# --------------------------------------------------------------------------- #
# the limits of their own
# --------------------------------------------------------------------------- #
def test_a_small_install_has_five_minutes(limits, tmp_path, monkeypatch):
    _handler().install(_apk(tmp_path, monkeypatch, 1))
    assert 300 < limits("install") < 320


def test_a_large_install_gets_the_time_a_slow_link_needs(limits, tmp_path, monkeypatch):
    _handler().install(_apk(tmp_path, monkeypatch, 1024))
    # 1 GB in five minutes would take 3.4 MB/s; a weak Wi-Fi link gives a tenth
    assert limits("install") >= 300 + 1024 * _MB / (512 * 1024)


def test_split_apks_count_together(limits, tmp_path, monkeypatch):
    base = _apk(tmp_path, monkeypatch, 1, "base.apk")
    _handler().install_multiple([base, base])
    small = limits("install-multiple")
    assert 600 < small < 640


def test_an_apk_that_is_missing_still_reaches_adb(limits, tmp_path):
    _handler().install(str(tmp_path / "nowhere.apk"))  # adb says what is wrong with it
    assert limits("install") == 300


def test_copy_and_delete_keep_their_own_limits(limits):
    dev = _handler()
    dev.copy("/sdcard/maps", "/sdcard/maps-copy")
    dev.remove("/sdcard/maps", recursive=True)
    assert limits("cp -r") == 600 and limits("rm -rf") == 300


# --------------------------------------------------------------------------- #
# command_timeout raises them, and None lifts them
# --------------------------------------------------------------------------- #
def test_no_command_timeout_means_no_limit(limits, tmp_path, monkeypatch):
    dev = _handler(command_timeout=None)
    dev.install(_apk(tmp_path, monkeypatch, 1))
    dev.install_multiple([str(tmp_path / "app.apk"), str(tmp_path / "app.apk")])
    dev.copy("/sdcard/maps", "/sdcard/maps-copy")
    dev.remove("/sdcard/maps", recursive=True)
    for word in ("install ", "install-multiple", "cp -r", "rm -rf"):
        assert limits(word) is None


@pytest.mark.parametrize("given, limit", [("0", None), ("3600", 3600)])
def test_the_command_lines_timeout_reaches_an_install(limits, tmp_path, monkeypatch, given, limit):
    import turboadb.cli as cli

    args = cli.build_parser().parse_args(["-s", "x", "--timeout", given, "install", "app.apk"])
    ADBHandler(cli._config(args)).install(_apk(tmp_path, monkeypatch, 1))
    assert limits("install") == limit


def test_a_longer_command_timeout_replaces_their_own(limits, tmp_path, monkeypatch):
    dev = _handler(command_timeout=7200)
    dev.install(_apk(tmp_path, monkeypatch, 1))
    dev.copy("/sdcard/maps", "/sdcard/maps-copy")
    dev.remove("/sdcard/maps", recursive=True)
    assert limits("install") == limits("cp -r") == limits("rm -rf") == 7200


def test_a_short_command_timeout_never_cuts_them(limits, tmp_path, monkeypatch):
    dev = _handler(command_timeout=5)  # for quick commands: an install is not one
    dev.install(_apk(tmp_path, monkeypatch, 1))
    dev.copy("/sdcard/maps", "/sdcard/maps-copy")
    assert limits("install") > 300 and limits("cp -r") == 600
