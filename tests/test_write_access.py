"""Making device files writable: the engine steps, the CLI, the choice dialog,
and terminals that follow adb root / unroot (an adb shell reopens)."""
import json

import pytest

from turboadb.config import ADBConfig
from turboadb.core import ADBHandler
from turboadb.exceptions import ADBCommandError, ADBError


# ---- engine ---------------------------------------------------------------- #
def test_access_status_reads_root_build_verity_and_bootloader(fake_adb):
    fake_adb.add("id -u", stdout="noise\n@@0|1|userdebug|enforcing|unlocked|\n")
    status = ADBHandler(ADBConfig(serial="S1")).access_status()
    assert status == {"root": True, "uid": 0, "debuggable": True, "build_type": "userdebug",
                      "verity": "enforcing", "bootloader": "unlocked"}
    assert fake_adb.argv_after_adb()[:3] == ["-s", "S1", "shell"]


def test_access_status_of_a_production_build(fake_adb):
    fake_adb.add("id -u", stdout="@@2000|0|user|||1\n")
    status = ADBHandler(ADBConfig(serial="S1")).access_status()
    assert status["root"] is False and status["uid"] == 2000
    assert status["debuggable"] is False and status["build_type"] == "user"
    assert status["verity"] == "" and status["bootloader"] == "locked"  # ro.boot.flash.locked=1


def test_access_status_failure_is_an_error_or_a_failed_result(fake_adb):
    fake_adb.add("id -u", returncode=1, stderr="error: device offline")
    handler = ADBHandler(ADBConfig(serial="S1"))
    with pytest.raises(ADBCommandError):
        handler.access_status()
    assert handler.access_status(safe=True).success is False


class _Steps:
    """Stubs the device-changing engine calls of one handler and records them."""

    def __init__(self, monkeypatch, handler, *, remount_outputs, disable_output="Verity disabled"):
        self.calls = []
        remount_outputs = list(remount_outputs)

        def record(name, value=True):
            def call(*args, **kwargs):
                self.calls.append(name)
                return value
            return call

        monkeypatch.setattr(handler, "root", record("root", "restarting adbd as root"))
        monkeypatch.setattr(handler, "disable_verity", record("disable-verity", disable_output))
        monkeypatch.setattr(handler, "reboot", record("reboot"))
        monkeypatch.setattr(handler, "wait_for_boot", record("wait_for_boot"))
        monkeypatch.setattr(handler, "shell", record("sync"))

        def remount_once():
            self.calls.append("remount")
            output = remount_outputs.pop(0)
            if isinstance(output, Exception):
                raise output
            return output

        monkeypatch.setattr(handler, "_remount_once", remount_once)


def test_make_writable_roots_and_remounts(monkeypatch):
    handler = ADBHandler(ADBConfig(serial="S1"))
    steps = _Steps(monkeypatch, handler, remount_outputs=[("remount succeeded", False)])
    notes = []
    report = handler.make_writable(on_step=notes.append)
    assert steps.calls == ["root", "remount"]
    assert report == {"steps": [("root", "restarting adbd as root"), ("remount", "remount succeeded")],
                      "rebooted": False, "reboot_needed": False}
    assert notes == ["adb root", "adb remount"]


def test_make_writable_reboots_for_verity_and_roots_again(monkeypatch):
    handler = ADBHandler(ADBConfig(serial="S1"))
    steps = _Steps(monkeypatch, handler, remount_outputs=[("remount succeeded", False)])
    report = handler.make_writable(disable_verity=True)
    # adbd starts without root after the reboot, so root is asked again
    assert steps.calls == ["root", "disable-verity", "sync", "reboot", "wait_for_boot", "root",
                           "remount"]
    assert report["rebooted"] and not report["reboot_needed"]


def test_make_writable_reboots_when_remount_asks_and_remounts_again(monkeypatch):
    handler = ADBHandler(ADBConfig(serial="S1"))
    steps = _Steps(monkeypatch, handler, remount_outputs=[
        ("Using overlayfs for /system\nNow reboot your device for settings to take effect", True),
        ("remount succeeded", False),
    ])
    report = handler.make_writable()
    assert steps.calls == ["root", "remount", "sync", "reboot", "wait_for_boot", "root", "remount"]
    assert [name for name, _out in report["steps"]] == ["root", "remount", "reboot", "root", "remount"]


def test_make_writable_without_reboot_reports_that_one_is_due(monkeypatch):
    handler = ADBHandler(ADBConfig(serial="S1"))
    steps = _Steps(monkeypatch, handler, remount_outputs=[("Now reboot your device", True)])
    report = handler.make_writable(disable_verity=True, reboot=False)
    assert "reboot" not in steps.calls and report["reboot_needed"] and not report["rebooted"]


def test_make_writable_skips_the_reboot_when_verity_was_already_off(monkeypatch):
    handler = ADBHandler(ADBConfig(serial="S1"))
    steps = _Steps(monkeypatch, handler, remount_outputs=[("remount succeeded", False)],
                   disable_output="Verity already disabled on /system")
    handler.make_writable(disable_verity=True)
    assert steps.calls == ["root", "disable-verity", "remount"]


def test_make_writable_stops_at_a_refused_step(monkeypatch):
    handler = ADBHandler(ADBConfig(serial="S1"))
    _Steps(monkeypatch, handler, remount_outputs=[ADBError("remount failed")])
    with pytest.raises(ADBError):
        handler.make_writable()
    steps = _Steps(monkeypatch, handler, remount_outputs=[ADBError("remount failed")])
    assert handler.make_writable(safe=True).success is False
    assert steps.calls == ["root", "remount"]


def test_remount_once_tells_a_reboot_request_from_a_refusal(fake_adb):
    handler = ADBHandler(ADBConfig(serial="S1"))
    fake_adb.add("remount", stdout="Disabling verity for /system\nNow reboot your device for "
                                   "settings to take effect\n")
    assert handler._remount_once()[1] is True
    fake_adb.rules.clear()
    fake_adb.add("remount", stdout="remount succeeded\n")
    assert handler._remount_once() == ("remount succeeded", False)
    fake_adb.rules.clear()
    fake_adb.add("remount", returncode=1, stderr="remount failed: not running as root")
    with pytest.raises(ADBCommandError):
        handler._remount_once()


# ---- CLI ------------------------------------------------------------------- #
class _Dev:
    config = ADBConfig()

    def __init__(self):
        self.kwargs = None

    def connect(self, *args, **kwargs):
        return True

    def access_status(self):
        return {"root": False, "uid": 2000, "debuggable": True, "build_type": "userdebug",
                "verity": "enforcing", "bootloader": "unlocked"}

    def make_writable(self, **kwargs):
        self.kwargs = kwargs
        if kwargs.get("on_step"):
            kwargs["on_step"]("adb root")
        return {"steps": [("root", "restarting adbd as root")], "rebooted": False,
                "reboot_needed": not kwargs["reboot"]}


@pytest.fixture
def run_cli(monkeypatch, capsys):
    import turboadb.cli as cli

    monkeypatch.setattr(cli, "adb_available", lambda *a, **k: True)
    monkeypatch.setattr(cli, "scrcpy_available", lambda *a, **k: True)

    def go(argv, dev):
        monkeypatch.setattr(cli, "_handler", lambda args, **kw: dev)
        rc = cli.main(argv)
        out, err = capsys.readouterr()
        return rc, out, err

    return go


def test_cli_access_prints_the_state_and_json(run_cli):
    rc, out, _err = run_cli(["-s", "S1", "access"], _Dev())
    assert rc == 0 and "adbd root:   no (uid 2000)" in out and "verity:      enforcing" in out
    rc, out, _err = run_cli(["-s", "S1", "access", "--json"], _Dev())
    assert rc == 0 and json.loads(out)["build_type"] == "userdebug"


def test_cli_make_writable_passes_the_choices(run_cli):
    dev = _Dev()
    rc, out, err = run_cli(["-s", "S1", "make-writable", "--disable-verity", "--no-reboot",
                            "--boot-timeout", "90"], dev)
    assert rc == 0
    assert {k: v for k, v in dev.kwargs.items() if k != "on_step"} == {
        "root": True, "disable_verity": True, "remount": True, "reboot": False,
        "boot_timeout": 90.0}
    assert "root: restarting adbd as root" in out
    assert "-> adb root" in err and "reboot is still needed" in err
    dev = _Dev()
    rc, out, _err = run_cli(["-s", "S1", "make-writable", "--no-root", "--no-remount", "--json"], dev)
    assert rc == 0 and dev.kwargs["root"] is False and dev.kwargs["remount"] is False
    assert dev.kwargs["on_step"] is None and json.loads(out)["ok"] is True


# ---- text checks ----------------------------------------------------------- #
def test_refusals_and_system_partitions_are_recognised():
    pytest.importorskip("PyQt5")
    from turboadb.gui import device_access as da

    for text in ("rm: /system/app/x: Read-only file system",
                 "adb: error: failed to copy 'a' to '/vendor/a': remote couldn't create file: "
                 "Permission denied",
                 "mkdir: '/data/x': Operation not permitted"):
        assert da.is_permission_problem(text), text
    assert not da.is_permission_problem("No such file or directory")
    assert not da.is_permission_problem("")
    assert da.refusal_line("\x1b[31mok\nmkdir: /system/x: Read-only file system\x1b[0m\n") == \
        "mkdir: /system/x: Read-only file system"
    assert da.refusal_line("all good\n") == ""
    assert da.on_system_partition("/system") and da.on_system_partition("/vendor/etc/a.xml")
    assert da.on_system_partition("/system_ext/priv-app")
    assert not da.on_system_partition("/systemx") and not da.on_system_partition("/sdcard/system")
    assert not da.on_system_partition("")


# ---- the dialog ------------------------------------------------------------ #
def test_dialog_preselects_the_steps_the_device_needs(qapp):
    from turboadb.gui.device_access import WriteAccessDialog

    status = {"root": False, "debuggable": True, "build_type": "userdebug",
              "verity": "enforcing", "bootloader": "unlocked"}
    dialog = WriteAccessDialog(status, path="/system/etc", error="Read-only file system",
                               action="saving hosts", can_retry=True)
    try:
        assert dialog.chk_root.isChecked() and dialog.chk_remount.isChecked()
        assert not dialog.chk_verity.isChecked() and dialog.chk_reboot.isChecked()
        assert dialog.chk_retry is not None and dialog.chk_retry.isChecked()
        assert dialog.choices() == {"root": True, "disable_verity": False, "remount": True,
                                    "reboot": True, "retry": True}
        dialog.chk_root.setChecked(False)
        dialog.chk_remount.setChecked(False)
        assert not dialog.btn_run.isEnabled()  # nothing to run
        dialog.chk_verity.setChecked(True)
        assert dialog.btn_run.isEnabled()
        assert dialog.choices()["disable_verity"] is True
    finally:
        dialog.deleteLater()


def test_dialog_for_a_rooted_device_with_verity_off(qapp):
    from turboadb.gui.device_access import WriteAccessDialog

    status = {"root": True, "debuggable": True, "build_type": "eng", "verity": "disabled",
              "bootloader": "locked"}
    dialog = WriteAccessDialog(status, path="/data/local/tmp", error="Permission denied")
    try:
        assert not dialog.chk_root.isChecked() and "already runs as root" in dialog.chk_root.text()
        assert not dialog.chk_verity.isEnabled() and dialog.choices()["disable_verity"] is False
        assert not dialog.chk_remount.isChecked()  # not a system partition, not read-only
        assert dialog.chk_retry is None and dialog.choices()["retry"] is False
        assert not dialog.btn_run.isEnabled()
    finally:
        dialog.deleteLater()


def test_dialog_without_a_known_state_still_offers_root_and_remount(qapp):
    from turboadb.gui.device_access import WriteAccessDialog, describe_status

    dialog = WriteAccessDialog(None)
    try:
        assert dialog.chk_root.isChecked() and dialog.chk_remount.isChecked()
        assert dialog.btn_run.isEnabled()
    finally:
        dialog.deleteLater()
    assert "could not be read" in describe_status(None)
    assert describe_status({"root": True, "build_type": "userdebug", "verity": "enforcing"}) == \
        "adbd: root   ·   userdebug build   ·   verity enforcing"


# ---- terminals follow adbd restarts ---------------------------------------- #
class _FakeSession:
    running = True

    def __init__(self):
        self.sent = []

    def send(self, data):
        self.sent.append(data)

    def close(self):
        self.running = False


@pytest.fixture
def local_shell(qapp, tmp_path, monkeypatch):
    from turboadb.gui.device_tab import _LocalShellWidget

    widget = _LocalShellWidget("powershell", serial="V2318")
    widget._shell_cwd = str(tmp_path)
    widget._started = True
    widget.session = _FakeSession()
    monkeypatch.setattr(widget, "_stop_session", lambda **_kw: None)
    try:
        yield widget
    finally:
        widget.close_panel()
        widget.deleteLater()


def _out(widget, text):
    widget._feed_from(widget.reader, text.encode("utf-8"))


def test_typed_adb_root_for_this_device_asks_the_tab_to_follow(local_shell):
    verbs = []
    local_shell.adbd_restart_requested.connect(verbs.append)
    local_shell._send(b"adb root\r\n")
    assert local_shell.session.sent[-1] == b"adb root\r\n" and verbs == ["root"]
    local_shell._send(b"adb -s V2318 unroot\r\n")
    local_shell._send(b"adb -s OTHER root\r\n")  # another device
    local_shell._send(b"adb root now\r\n")  # not the adb root command
    assert verbs == ["root", "unroot"]
    assert local_shell._local_adb_reboot_mode(["adb", "-s", "V2318", "reboot", "bootloader"]) == \
        "bootloader"


def test_an_adb_shell_reopens_in_its_folder_after_adbd_restarts(local_shell, tmp_path):
    shell = local_shell
    shell._send(b"adb shell\r\n")
    _out(shell, "PD2318:/ $ ")
    shell._send(b"cd /sdcard/Download\r\n")
    _out(shell, "PD2318:/sdcard/Download $ ")

    shell.hold_adb_shell()
    _out(shell, f"\r\nPS {tmp_path}> ")  # adbd restarted: the adb shell ended
    assert not shell._in_adb_shell and shell.session.sent[-1] == b"cd /sdcard/Download\r\n"

    shell.resume_adb_shell()
    assert shell.session.sent[-1] == b"adb shell -t -t\r\n" and shell._in_adb_shell
    assert shell._adb_shell_cwd == "/sdcard/Download"
    _out(shell, "PD2318:/ # ")  # now root
    assert shell.session.sent[-1] == b"cd /sdcard/Download\r\n"


def test_resume_waits_for_the_pc_prompt_and_never_types_into_a_live_adb_shell(local_shell, tmp_path):
    shell = local_shell
    shell._send(b"adb shell\r\n")
    _out(shell, "PD2318:/ $ ")
    shell.hold_adb_shell()
    sent = len(shell.session.sent)
    shell.resume_adb_shell()  # "already running as root": the adb shell is still there
    assert len(shell.session.sent) == sent and shell._in_adb_shell
    _out(shell, f"\r\nPS {tmp_path}> ")  # it ends a moment later
    assert shell.session.sent[-1] == b"adb shell -t -t\r\n"


def test_typing_or_no_hold_means_no_reopen(local_shell, tmp_path):
    shell = local_shell
    shell.resume_adb_shell()  # nothing held
    assert shell.session.sent == []
    shell._send(b"adb shell\r\n")
    _out(shell, "PD2318:/ $ ")
    shell.hold_adb_shell()
    _out(shell, f"\r\nPS {tmp_path}> ")
    shell._send(b"dir\r\n")  # the user moved on
    shell.resume_adb_shell()
    assert shell.session.sent[-1] == b"dir\r\n"


def test_a_device_reboot_reopens_the_adb_shell_once_the_device_is_back(qapp, local_shell):
    shell = local_shell
    shell._send(b"adb shell\r\n")
    _out(shell, "PD2318:/ $ ")
    shell._send(b"cd /sdcard\r\n")
    # ShellPanel.pause_for_device_reboot does exactly this for PowerShell and CMD
    shell.hold_adb_shell()
    shell.reset_adb_shell_context()
    shell.resume_adb_shell()
    assert shell.session.sent[-1] == b"adb shell -t -t\r\n" and shell._adb_shell_cwd == "/sdcard"


def test_a_refusal_inside_adb_shell_is_reported_once_a_minute(local_shell, monkeypatch):
    import turboadb.gui.device_tab as device_tab

    lines = []
    local_shell.access_refused.connect(lines.append)
    clock = [1000.0]
    monkeypatch.setattr(device_tab.time, "monotonic", lambda: clock[0])
    _out(local_shell, "rm: /system/x: Read-only file system\r\n")  # not in adb shell: PC output
    assert lines == []
    local_shell._send(b"adb shell\r\n")
    _out(local_shell, "PD2318:/ $ ")
    _out(local_shell, "rm: /system/x: Read-only file system\r\nPD2318:/ $ ")
    _out(local_shell, "rm: /system/y: Read-only file system\r\nPD2318:/ $ ")
    assert lines == ["rm: /system/x: Read-only file system"]
    clock[0] += 61
    _out(local_shell, "touch: '/vendor/z': Permission denied\r\n")
    assert lines[-1] == "touch: '/vendor/z': Permission denied"
