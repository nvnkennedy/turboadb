"""Installing, uninstalling and clearing apps: what is sent to adb and to the
device's package manager, and how their answers are read."""
import pytest

from turboadb import ADBConfig, ADBHandler
from turboadb.exceptions import ADBCommandError, ADBInstallError

# adb's own client refuses `adb uninstall -k` before it contacts the device.
_ADB_REFUSES_K = (
    "The -k option uninstalls the application while retaining the data/cache.\n"
    "At the moment, there is no way to remove the remaining data.\n"
    "You will have to reinstall the application with the same signature, and fully "
    "uninstall it.\n"
    "If you truly wish to continue, execute 'adb shell cmd package uninstall -k'.\n"
)


def _adb_uninstall_k(argv):
    return "uninstall" in argv and "-k" in argv and "shell" not in argv


def _shell_commands(fake_adb):
    return [" ".join(argv[argv.index("shell") + 1:]) for argv in fake_adb.calls
            if "shell" in argv]


# --------------------------------------------------------------------------- #
# uninstall
# --------------------------------------------------------------------------- #
def test_uninstall_goes_through_adb(fake_adb):
    fake_adb.add("uninstall", stdout="Success\n")
    dev = ADBHandler(ADBConfig(serial="S1"))
    assert dev.uninstall("com.example.app") == "Success"
    assert fake_adb.argv_after_adb() == ["-s", "S1", "uninstall", "com.example.app"]


def test_uninstall_keeping_the_data_asks_the_package_manager_itself(fake_adb):
    fake_adb.add(_adb_uninstall_k, stdout=_ADB_REFUSES_K, returncode=1)
    fake_adb.add("cmd package uninstall -k", stdout="Success\n")
    dev = ADBHandler(ADBConfig(serial="S1"))
    assert dev.uninstall("com.example.app", keep_data=True) == "Success"
    assert _shell_commands(fake_adb) == ["cmd package uninstall -k com.example.app"]
    assert not any(_adb_uninstall_k(argv) for argv in fake_adb.calls)


def test_uninstall_keeping_the_data_uses_pm_where_cmd_is_missing(fake_adb):
    fake_adb.add("cmd package", stdout="/system/bin/sh: cmd: not found\n", returncode=127)
    fake_adb.add("pm uninstall -k", stdout="Success\n")
    dev = ADBHandler(ADBConfig(serial="S1"))
    assert dev.uninstall("com.example.app", keep_data=True) == "Success"
    assert _shell_commands(fake_adb) == ["cmd package uninstall -k com.example.app",
                                         "pm uninstall -k com.example.app"]


def test_uninstall_keeping_the_data_uses_pm_where_cmd_cannot_reach_the_package_manager(fake_adb):
    # cmd's own complaint holds the word "Failure": it is not the package manager's answer
    fake_adb.add("cmd package", returncode=224,
                 stdout="cmd: Failure calling service package: Broken pipe (32)\n")
    fake_adb.add("pm uninstall -k", stdout="Success\n")
    dev = ADBHandler(ADBConfig(serial="S1"))
    assert dev.uninstall("com.example.app", keep_data=True) == "Success"
    assert _shell_commands(fake_adb)[-1] == "pm uninstall -k com.example.app"


def test_a_refused_uninstall_keeping_the_data_is_reported_as_the_device_said_it(fake_adb):
    fake_adb.add("cmd package uninstall -k", returncode=1,
                 stdout="Failure [DELETE_FAILED_INTERNAL_ERROR]\n")
    dev = ADBHandler(ADBConfig(serial="S1"))
    with pytest.raises(ADBInstallError, match="DELETE_FAILED_INTERNAL_ERROR"):
        dev.uninstall("com.example.app", keep_data=True)
    assert _shell_commands(fake_adb) == ["cmd package uninstall -k com.example.app"]  # no pm


def test_the_package_name_is_quoted_for_the_device_shell(fake_adb):
    fake_adb.add("cmd package uninstall -k", stdout="Success\n")
    ADBHandler(ADBConfig(serial="S1")).uninstall("com.example; reboot", keep_data=True)
    assert fake_adb.argv_after_adb()[-1] == "'com.example; reboot'"


# --------------------------------------------------------------------------- #
# install: the package manager's answer is a line of its own
# --------------------------------------------------------------------------- #
def test_an_install_adb_reports_as_done_succeeds(fake_adb):
    fake_adb.add("install", stdout="Performing Streamed Install\r\nSuccess\r\n")
    dev = ADBHandler(ADBConfig(serial="S1"))
    assert "Success" in dev.install("app.apk")
    assert "Success" in dev.install_multiple(["base.apk", "split_config.en.apk"])


def test_a_refused_install_is_a_failure_whatever_the_apk_is_called(fake_adb):
    # adb names the file in its error, and "Success" is part of this name
    fake_adb.add(
        "install", stdout="Performing Streamed Install\n", returncode=1,
        stderr="adb: failed to install SuccessFactors.apk: "
               "Failure [INSTALL_FAILED_VERSION_DOWNGRADE]",
    )
    dev = ADBHandler(ADBConfig(serial="S1"))
    with pytest.raises(ADBInstallError, match="INSTALL_FAILED_VERSION_DOWNGRADE"):
        dev.install("SuccessFactors.apk")
    with pytest.raises(ADBInstallError, match="INSTALL_FAILED_VERSION_DOWNGRADE"):
        dev.install_multiple(["SuccessFactors.apk", "split_config.en.apk"])


def test_a_staged_install_is_done_when_the_package_manager_says_so(fake_adb):
    # an APEX module, or --staged: the answer goes on after the word
    staged = "Performing Streamed Install\nSuccess. Reboot device to apply staged session\n"
    fake_adb.add("install", stdout=staged)
    dev = ADBHandler(ADBConfig(serial="S1"))
    assert dev.install("com.android.tzdata.apex").endswith("apply staged session")
    assert dev.install_multiple(["a.apex", "b.apex"]).endswith("apply staged session")


@pytest.mark.parametrize("apk", ["SuccessFactors.apk", "Success.apk", "Success build.apk"])
def test_a_refused_install_is_a_failure_where_adb_exits_zero_all_the_same(fake_adb, apk):
    # an old device: adb pushes the file first, and its summary line starts
    # with the file's name; no exit status comes back from the package manager
    fake_adb.add("install", stdout=(
        f"{apk}: 1 file pushed, 0 skipped. 31.2 MB/s (5120 bytes in 0.001s)\n"
        f"\tpkg: /data/local/tmp/{apk}\n"
        "Failure [INSTALL_FAILED_VERSION_DOWNGRADE]\n"))
    with pytest.raises(ADBInstallError, match="INSTALL_FAILED_VERSION_DOWNGRADE"):
        ADBHandler(ADBConfig(serial="S1")).install(apk)


def test_an_install_adb_ended_with_an_error_is_a_failure(fake_adb):
    fake_adb.add("install", stdout="Success\n", returncode=1, stderr="adb: device offline")
    with pytest.raises(ADBInstallError, match="device offline"):
        ADBHandler(ADBConfig(serial="S1")).install("app.apk")


def test_a_refused_uninstall_is_a_failure_whatever_else_is_printed(fake_adb):
    fake_adb.add("uninstall", returncode=1, stdout="Failure [DELETE_FAILED_INTERNAL_ERROR]\n",
                 stderr="Successfully reached the device\n")
    with pytest.raises(ADBInstallError, match="DELETE_FAILED_INTERNAL_ERROR"):
        ADBHandler(ADBConfig(serial="S1")).uninstall("com.example.app")


# --------------------------------------------------------------------------- #
# whose app: on a car, user 0 is the hidden system user and the driver is 10
# --------------------------------------------------------------------------- #
_CAMERA = "android.permission.CAMERA"


def _car(fake_adb, *, driver_has=("com.example.app",)):
    """A head unit whose foreground user is 10, with *driver_has* installed
    for that user; ``pm clear`` answers Success."""
    fake_adb.add("get-current-user", stdout="10\n")
    for package in driver_has:
        fake_adb.add(f"pm path --user 10 {package}",
                     stdout=f"package:/data/app/{package}/base.apk\n")
    fake_adb.add("pm path", returncode=1)  # not installed for that user
    fake_adb.add("pm clear", stdout="Success\n")
    return ADBHandler(ADBConfig(serial="S1"))


def test_clear_data_clears_the_drivers_app_on_a_car(fake_adb):
    dev = _car(fake_adb)
    assert dev.clear_app("com.example.app") == "Success"
    assert _shell_commands(fake_adb)[-1] == "pm clear --user 10 com.example.app"


def test_grant_and_revoke_are_for_the_drivers_app_on_a_car(fake_adb):
    dev = _car(fake_adb)
    assert dev.grant("com.example.app", _CAMERA) is True
    assert _shell_commands(fake_adb)[-1] == f"pm grant --user 10 com.example.app {_CAMERA}"
    assert dev.revoke("com.example.app", _CAMERA) is True
    assert _shell_commands(fake_adb)[-1] == f"pm revoke --user 10 com.example.app {_CAMERA}"
    # the foreground user is asked once, not before every command
    assert _shell_commands(fake_adb).count("am get-current-user") == 1


def test_an_app_only_the_system_user_has_is_handled_as_before(fake_adb):
    dev = _car(fake_adb, driver_has=())
    assert dev.clear_app("com.vendor.service") == "Success"
    assert _shell_commands(fake_adb)[-1] == "pm clear com.vendor.service"
    dev.grant("com.vendor.service", _CAMERA)
    assert _shell_commands(fake_adb)[-1] == f"pm grant com.vendor.service {_CAMERA}"


@pytest.mark.parametrize("answer", ["0\n", "", "Unknown command: get-current-user\n"])
def test_a_phone_keeps_the_plain_commands(fake_adb, answer):
    fake_adb.add("get-current-user", stdout=answer)
    fake_adb.add("pm clear", stdout="Success\n")
    dev = ADBHandler(ADBConfig(serial="S1"))
    dev.clear_app("com.example.app")
    dev.grant("com.example.app", _CAMERA)
    dev.revoke("com.example.app", _CAMERA)
    assert _shell_commands(fake_adb) == [
        "am get-current-user",
        "pm clear com.example.app",
        f"pm grant com.example.app {_CAMERA}",
        f"pm revoke com.example.app {_CAMERA}",
    ]


def test_a_refused_clear_says_whose_data_it_was_about(fake_adb):
    fake_adb.add("get-current-user", stdout="10\n")
    fake_adb.add("pm path", stdout="package:/data/app/com.example.app/base.apk\n")
    fake_adb.add("pm clear", stdout="Failed\n")  # exits 0 on a device without exit codes
    with pytest.raises(ADBCommandError, match="pm clear --user 10 com.example.app"):
        ADBHandler(ADBConfig(serial="S1")).clear_app("com.example.app")
