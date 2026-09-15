"""Device classification: phone, tablet, TV, watch, Android Automotive or an
Android infotainment head unit."""
from turboadb import ADBConfig, ADBHandler


def _output(features=(), characteristics="default", wm="Physical size: 1080x2400"):
    lines = [f"feature:{name}" for name in features]
    return "\n".join(lines + ["__turboadb_ch__", characteristics, "__turboadb_wm__", wm])


def test_android_automotive_is_detected_from_the_feature():
    kind = ADBHandler.classify_device(
        _output(["android.hardware.type.automotive", "android.hardware.wifi"],
                wm="Physical size: 1920x720")
    )
    assert kind["kind"] == "automotive" and kind["automotive"] is True
    assert kind["display_size"] == "1920x720"
    assert "android.hardware.type.automotive" in kind["reason"]


def test_automotive_build_characteristic_alone_is_enough():
    kind = ADBHandler.classify_device(_output(["android.hardware.wifi"], "automotive,nosdcard"))
    assert kind["kind"] == "automotive"


def test_head_unit_without_aaos_is_detected_by_landscape_and_no_telephony():
    kind = ADBHandler.classify_device(
        _output(["android.hardware.wifi", "android.hardware.bluetooth"], "default",
                "Physical size: 1024x600")
    )
    assert kind["kind"] == "headunit" and kind["automotive"] is True
    assert kind["telephony"] is False


def test_phone_tablet_tv_and_watch():
    phone = ADBHandler.classify_device(
        _output(["android.hardware.telephony", "android.hardware.telephony.gsm"])
    )
    assert phone["kind"] == "phone" and phone["automotive"] is False and phone["telephony"]
    tablet = ADBHandler.classify_device(
        _output(["android.hardware.wifi"], "tablet", "Physical size: 2560x1600")
    )
    assert tablet["kind"] == "tablet"
    tv = ADBHandler.classify_device(
        _output(["android.software.leanback"], "tv", "Physical size: 1920x1080")
    )
    assert tv["kind"] == "tv" and tv["automotive"] is False
    watch = ADBHandler.classify_device(_output(["android.hardware.type.watch"], "watch"))
    assert watch["kind"] == "watch"


def test_override_size_wins_and_unknown_output_is_a_phone():
    kind = ADBHandler.classify_device(
        _output(["android.hardware.telephony"],
                wm="Physical size: 1440x3120\nOverride size: 1080x2340")
    )
    assert kind["display_size"] == "1080x2340"
    assert ADBHandler.classify_device("")["kind"] == "phone"


def test_device_info_includes_the_kind(fake_adb):
    fake_adb.add("shell getprop", stdout="[ro.product.model]: [IVI]\n")
    fake_adb.add(
        "pm list features",
        stdout=_output(["android.hardware.type.automotive"], wm="Physical size: 1920x720"),
    )
    info = ADBHandler(ADBConfig(serial="x")).device_info()
    assert info["kind"] == "automotive" and info["automotive"] is True
    assert info["display_size"] == "1920x720"
    assert info["kind_label"] == "Android Automotive head unit"


def test_device_kind_safe_mode_never_raises(fake_adb):
    fake_adb.add("pm list features", stdout=_output(["android.hardware.telephony"]))
    result = ADBHandler(ADBConfig(serial="x")).device_kind(safe=True)
    assert result.success and result.value["kind"] == "phone"
