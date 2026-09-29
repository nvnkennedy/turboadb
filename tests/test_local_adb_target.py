"""An ``adb -s SERIAL reboot`` (or root/unroot) typed in PowerShell/CMD is
this tab's own only when SERIAL is this tab's device.  A tab that does not
know its serial can't tell, so such a line never starts its reboot
recovery, which paused the terminals and waited for a device that never
went away."""

import pytest

pytest.importorskip("PyQt5")


@pytest.fixture
def widget(qapp):
    from turboadb.gui.device_tab import _LocalShellWidget

    made = []

    def make(serial):
        widget = _LocalShellWidget("cmd", serial=serial)
        made.append(widget)
        return widget

    yield make
    for w in made:
        w.close_panel()
        w.deleteLater()


def test_a_named_device_is_this_tabs_only_when_it_is_its_serial(widget):
    mine = widget("V2318")
    assert mine._local_adb_reboot_mode(["adb", "-s", "V2318", "reboot"]) == ""
    assert mine._local_adb_reboot_mode(["adb", "-s", "OTHER", "reboot", "recovery"]) is None
    assert mine._local_adbd_restart_verb(["adb", "-s", "OTHER", "root"]) is None
    unknown = widget(None)
    assert unknown._local_adb_reboot_mode(["adb", "-s", "OTHER", "reboot"]) is None
    assert unknown._local_adbd_restart_verb(["adb", "-s", "OTHER", "root"]) is None
    # without -s adb picks the one device there is: the tab's
    assert unknown._local_adb_reboot_mode(["adb", "reboot", "bootloader"]) == "bootloader"
    assert unknown._local_adbd_restart_verb(["adb", "root"]) == "root"
