"""Making /system writable survives adbd restarting, and an update says what it missed.

``adb root`` returns while the old adbd is still up, so the next command met it
going away and failed now and then.  And a TurboADB update that could not
update adb or scrcpy finished quietly: the user is told, with what to run.

No device is used: FakeAdb answers.
"""
import time
import types

import pytest

from turboadb import core, toolsdl, update
from turboadb.config import ADBConfig
from turboadb.core import ADBHandler
from turboadb.exceptions import ADBCommandError


class _Clock:
    """``time`` for core alone.  Patched on the real module, every thread still
    running from an earlier test would spin through it as well."""

    def __getattr__(self, name):
        return getattr(time, name)

    @staticmethod
    def sleep(_seconds):
        pass


@pytest.fixture(autouse=True)
def _no_waiting(monkeypatch):
    monkeypatch.setattr(core, "time", _Clock())


def _handler():
    return ADBHandler(ADBConfig(serial="x"))


def _commands(fake, text):
    return [c for c in fake.calls if text in " ".join(c)]


def _rule(fake, needle, **answer):
    """*needle* among adb's arguments.  (FakeAdb's own text match also sees adb's
    path, in a temporary folder named after the test.)"""
    fake.add(lambda argv: needle in " ".join(argv[1:]), **answer)


def _answers(fake, needle, *script):
    """*needle* answers from *script* in turn: ``(returncode, stdout, stderr)``,
    the last one for ever."""
    seen = []

    def pick(argv):
        return needle in " ".join(argv[1:])

    original = fake._answer

    def answer(argv):
        if pick(argv):
            seen.append(1)
            return script[min(len(seen) - 1, len(script) - 1)]
        return original(argv)

    fake._answer = answer
    return seen


# ---- adb root -----------------------------------------------------------------
def test_root_waits_until_adbd_has_really_restarted(fake_adb):
    _rule(fake_adb, "root", stdout="restarting adbd as root")
    asked = _answers(fake_adb, "id -u", (0, b"2000\n", b""), (1, b"", b"error: closed"), (0, b"0\n", b""))
    _handler().root()
    assert len(asked) == 3


def test_root_already_there_does_not_wait(fake_adb):
    _rule(fake_adb, "root", stdout="adbd is already running as root")
    _handler().root()
    assert _commands(fake_adb, "id -u") == []


def test_unroot_waits_for_the_other_way_round(fake_adb):
    _rule(fake_adb, "unroot", stdout="restarting adbd as non root")
    asked = _answers(fake_adb, "id -u", (0, b"0\n", b""), (0, b"2000\n", b""))
    _handler().unroot()
    assert len(asked) == 2


def test_an_adbd_that_never_changes_does_not_hang(fake_adb, monkeypatch):
    _rule(fake_adb, "root", stdout="restarting adbd as root")
    _answers(fake_adb, "id -u", (0, b"2000\n", b""))
    ticks = iter(range(0, 1000, 10))
    monkeypatch.setattr(core.time, "monotonic", lambda: next(ticks))
    _handler().root()  # returns; the next command reports what is wrong


def test_a_production_build_says_so(fake_adb):
    _rule(fake_adb, "root", stderr="adbd cannot run as root in production builds")
    with pytest.raises(ADBCommandError) as caught:
        _handler().root()
    assert "cannot run as root" in str(caught.value)


# ---- adb remount --------------------------------------------------------------
def test_remount_rides_out_adbd_restarting(fake_adb):
    asked = _answers(fake_adb, "remount", (1, b"", b"error: closed"), (0, b"remount succeeded", b""))
    assert "remount succeeded" in _handler().remount()
    assert len(asked) == 2


def test_a_real_refusal_is_not_asked_again(fake_adb):
    asked = _answers(fake_adb, "remount", (1, b"", b"remount failed: verity"))
    with pytest.raises(ADBCommandError):
        _handler().remount()
    assert len(asked) == 1


def test_remount_gets_root_first_where_it_was_missing(fake_adb):
    _rule(fake_adb, " root", stdout="restarting adbd as root")
    _answers(fake_adb, "remount", (1, b"", b"Not running as root. Try \"adb root\" first."),
             (0, b"remount succeeded", b""))
    assert "remount succeeded" in _handler().remount()
    assert _commands(fake_adb, "root")


def test_remount_on_a_production_build_says_why(fake_adb):
    _rule(fake_adb, " root", stderr="adbd cannot run as root in production builds")
    _answers(fake_adb, "remount", (1, b"", b"Not running as root. Try \"adb root\" first."))
    with pytest.raises(ADBCommandError) as caught:
        _handler().remount()
    assert "cannot run as root" in str(caught.value)


# ---- mount -o remount,rw ------------------------------------------------------
def test_mount_gets_root_first(fake_adb):
    _rule(fake_adb, " root", stdout="restarting adbd as root")
    assert _handler().mount_rw() == "ok"
    order = [" ".join(c) for c in fake_adb.calls]
    assert any("root" in c for c in order[:2])


def test_mount_still_tries_without_adb_root(fake_adb):
    _rule(fake_adb, " root", stderr="adbd cannot run as root in production builds")
    assert _handler().mount_rw() == "ok"
    assert _commands(fake_adb, "remount,rw")


def test_a_read_only_image_goes_through_adb_remount(fake_adb):
    """Android 10+: mount cannot make these writable, overlayfs can."""
    _rule(fake_adb, "remount,rw", stdout="mount: '/dev/block/dm-0' is read-only")
    _rule(fake_adb, "remount", stdout="Using overlayfs for /system\nremount succeeded")
    assert "remount succeeded" in _handler().mount_rw()


def test_when_both_fail_the_mount_error_is_the_one_shown(fake_adb):
    _rule(fake_adb, "remount,rw", stdout="mount: '/dev/block/dm-13' not user mountable in fstab")
    _rule(fake_adb, "remount", returncode=1, stderr="remount failed")
    with pytest.raises(ADBCommandError) as caught:
        _handler().mount_rw()
    assert "not user mountable" in str(caught.value)


def test_another_path_stays_with_mount(fake_adb):
    _rule(fake_adb, "remount,rw", stdout="mount: '/data/x' not in /proc/mounts")
    with pytest.raises(ADBCommandError):
        _handler().mount_rw("/data/x")
    assert not [c for c in fake_adb.calls if c[-1] == "remount"]


# ---- an update that could not update the tools --------------------------------
def test_by_hand_means_winget_where_there_is_one(monkeypatch):
    monkeypatch.setattr(toolsdl, "winget_exe", lambda: "winget.exe")
    assert toolsdl.manual_update_steps("adb") == "winget upgrade --id Google.PlatformTools --exact"
    assert toolsdl.manual_update_steps("scrcpy") == "winget upgrade --id Genymobile.scrcpy --exact"


def test_by_hand_means_the_publisher_elsewhere(monkeypatch):
    monkeypatch.setattr(toolsdl, "winget_exe", lambda: None)
    assert "developer.android.com" in toolsdl.manual_update_steps("adb")
    assert "github.com/Genymobile" in toolsdl.manual_update_steps("scrcpy")


def _upgraded(monkeypatch, report):
    monkeypatch.setattr(update, "can_self_update", lambda: True)
    monkeypatch.setattr(update, "current_version", lambda: "5.0.0")
    monkeypatch.setattr(update.subprocess, "run",
                        lambda *a, **k: types.SimpleNamespace(returncode=0, stdout="", stderr=""))
    monkeypatch.setattr(update, "_fresh_report", lambda *a, **k: report)
    said = []
    return update.run_upgrade(notify=said.append, expected="6.0.0"), said


def test_turboadb_is_updated_even_if_a_tool_is_not(monkeypatch):
    res, said = _upgraded(monkeypatch, {
        "version": "6.0.0", "adb": "37.0.2", "scrcpy": "4.1",
        "errors": {"scrcpy": "winget could not update scrcpy"},
        "manual": {"scrcpy": "winget upgrade --id Genymobile.scrcpy --exact"},
    })
    assert res["ok"] and res["new"] == "6.0.0"
    assert res["tools_manual"] == {"scrcpy": "winget upgrade --id Genymobile.scrcpy --exact"}
    assert any("Update scrcpy yourself: winget upgrade" in line for line in said)


def test_a_clean_update_has_nothing_to_say(monkeypatch):
    res, said = _upgraded(monkeypatch, {"version": "6.0.0", "adb": "37.0.2", "scrcpy": "5.0.1", "errors": {}})
    assert res["ok"] and "tools_manual" not in res
    assert not any("yourself" in line for line in said)


def test_an_update_check_that_could_not_be_made_counts_too():
    assert "could not find out whether a newer version exists" in update._REPORT_CODE
    assert "manual_update_steps" in update._REPORT_CODE


def test_the_window_tells_the_user(monkeypatch):
    pytest.importorskip("PyQt5")
    from turboadb.gui import main_window as mw

    shown = []
    monkeypatch.setattr(mw.QMessageBox, "information", lambda _p, _t, text: shown.append(text))
    monkeypatch.setattr(update, "relaunch_on_exit", lambda: False)
    fake = types.SimpleNamespace(_upd_dlg=None, _log=lambda *_a: None,
                                 _resume_after_self_update=lambda: None)
    mw.MainWindow._on_self_update_done(fake, {
        "ok": True, "new": "6.0.0", "adb": "37.0.2", "scrcpy": "4.1",
        "tools_errors": {"scrcpy": "boom"},
        "tools_manual": {"scrcpy": "winget upgrade --id Genymobile.scrcpy --exact"},
    })
    assert "Updated to TurboADB 6.0.0" in shown[0]
    assert "scrcpy could not be updated" in shown[0]
    assert "winget upgrade --id Genymobile.scrcpy --exact" in shown[0]
