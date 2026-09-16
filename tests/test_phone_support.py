"""What a device offers for calls and messages, and a clear error when the app
for a phone action is missing (customised head units)."""
import pytest

from turboadb import ADBConfig, ADBHandler
from turboadb.exceptions import ADBError

PHONE = """@@features
feature:android.hardware.bluetooth
feature:android.hardware.telephony
feature:android.hardware.telephony.calling
@@dial
priority=0 preferredOrder=0 match=0x208000 specificIndex=-1 isDefault=true
com.android.contacts/com.android.dialer.TwelveKeyDialer
@@call
priority=1 preferredOrder=0 match=0x208000 specificIndex=-1 isDefault=true
com.android.server.telecom/.components.UserCallActivity
@@sms
priority=0 preferredOrder=0 match=0x208000 specificIndex=-1 isDefault=true
com.google.android.apps.messaging/.ui.conversation.LaunchConversationActivity
@@apps
2 activities found:
  Activity #0:
    priority=0 preferredOrder=0 match=0x108000 specificIndex=-1 isDefault=true
    com.android.chrome/com.google.android.apps.chrome.Main
  Activity #1:
    priority=0 preferredOrder=0 match=0x108000 specificIndex=-1 isDefault=true
    com.android.contacts/com.android.dialer.TwelveKeyDialer
"""

HEAD_UNIT = """@@features
feature:android.hardware.bluetooth
feature:android.hardware.type.automotive
@@dial
No activity found
@@call
No activity found
@@sms
No activity found
@@apps
2 activities found:
  Activity #0:
    priority=0 preferredOrder=0 match=0x108000 specificIndex=-1 isDefault=true
    com.oem.navigation/.MainActivity
  Activity #1:
    priority=0 preferredOrder=0 match=0x108000 specificIndex=-1 isDefault=true
    com.oem.btphone/.HandsfreeActivity
"""


def test_phone_support_of_a_phone():
    info = ADBHandler.parse_phone_support(PHONE)
    assert info == {
        "telephony": True,
        "dialer": "com.android.contacts/com.android.dialer.TwelveKeyDialer",
        "caller": "com.android.server.telecom/.components.UserCallActivity",
        "messages": "com.google.android.apps.messaging/.ui.conversation.LaunchConversationActivity",
        "phone_apps": ["com.android.contacts"],
    }


def test_phone_support_of_a_customised_head_unit():
    info = ADBHandler.parse_phone_support(HEAD_UNIT)
    assert info == {
        "telephony": False,
        "dialer": "",
        "caller": "",
        "messages": "",
        "phone_apps": ["com.oem.btphone"],
    }


def test_only_real_phone_apps_are_offered():
    looks = ADBHandler._looks_like_phone_app
    assert looks("com.oem.btphone/.HandsfreeActivity")
    assert looks("com.android.car.dialer/.ui.TelecomActivity")
    assert looks("com.android.incallui/.InCallActivity")
    assert looks("com.oem.hfp/.MainActivity")
    # a name that merely contains "phone" is not a phone app
    assert not looks("com.phonepe.app/.Navigator_MainActivity")
    assert not looks("com.oem.phonebook/.ContactsActivity")
    assert not looks("com.android.chrome/com.google.android.apps.chrome.Main")


def test_phone_support_is_unknown_when_the_device_cannot_say():
    old = "@@features\n@@dial\nUnknown command: resolve-activity\n@@call\n@@sms\n@@apps\n"
    info = ADBHandler.parse_phone_support(old)
    assert info["telephony"] is None
    assert info["dialer"] is None and info["caller"] is None and info["messages"] is None
    assert info["phone_apps"] == []


def test_phone_support_asks_the_device_in_one_query(fake_adb):
    fake_adb.add("resolve-activity", stdout=HEAD_UNIT)
    info = ADBHandler(ADBConfig(serial="ivi")).phone_support()
    assert info["dialer"] == "" and info["phone_apps"] == ["com.oem.btphone"]


def test_dialling_without_a_dialler_app_says_so(fake_adb):
    fake_adb.add(
        "am start",
        stdout=(
            "Starting: Intent { act=android.intent.action.DIAL dat=tel:xxx }\n"
            "Error: Activity not started, unable to resolve Intent "
            "{ act=android.intent.action.DIAL dat=tel:xxx flg=0x10000000 }\n"
        ),
        returncode=1,
    )
    with pytest.raises(ADBError, match="No app on this device handles the dialler"):
        ADBHandler(ADBConfig(serial="ivi")).dial("1800123456")
    res = ADBHandler(ADBConfig(serial="ivi"), safe=True).call("1800123456")
    assert not res.success and "phone calls" in str(res.error)


def test_a_started_dialler_still_reports_success(fake_adb):
    fake_adb.add("am start", stdout="Starting: Intent { act=android.intent.action.DIAL dat=tel:xxx }\n")
    assert ADBHandler(ADBConfig(serial="x")).dial("1800123456") is True
