"""A device adb connected through mDNS (Wireless debugging) is a network device.

adb names such a device after its service, ``adb-SERIAL-xxxxxx._adb-tls-connect._tcp``,
with no ``:port`` in it.  "Network" was judged by a colon in the serial, so the
device list labelled it USB and ``disconnect()`` never passed it to adb.  adb 37
itself can't disconnect that name (it reads it as ``NAME:5555``); the handler
then says so instead of reporting a disconnect that did not happen.
"""
import pytest

from turboadb.config import ADBConfig
from turboadb.core import ADBHandler
from turboadb.devices import Device, is_mdns_serial, is_network_serial

_MDNS = "adb-R58M123ABC-AbCdEf._adb-tls-connect._tcp"


@pytest.mark.parametrize("serial", [
    _MDNS,
    _MDNS + ".",  # some adb versions end the service name with a dot
    "adb-R58M123ABC-AbCdEf._adb._tcp",  # the older, plain service
    "192.168.0.5:5555",
    "[fe80::1]:5555",
])
def test_a_network_device_is_labelled_so(serial):
    device = Device(serial, "device", model="Pixel_7")
    assert is_network_serial(serial) and device.is_network
    assert device.label == "Pixel_7 [net]"


@pytest.mark.parametrize("serial", ["R58M123ABC", "emulator-5554", "usb:3-1", "", None])
def test_a_usb_device_is_not(serial):
    assert not is_network_serial(serial)
    assert not is_mdns_serial(serial)


def _handler(serial, messages):
    return ADBHandler(ADBConfig(serial=serial), log_callback=messages.append)


def test_a_wireless_debugging_device_is_handed_to_adb_disconnect(fake_adb):
    fake_adb.add("disconnect", stdout=f"disconnected {_MDNS}\n")
    messages = []
    assert _handler(_MDNS, messages).disconnect() is True
    assert fake_adb.argv_after_adb() == ["disconnect", _MDNS]
    assert any(f"Disconnected {_MDNS}." in m for m in messages)


def test_a_refused_disconnect_of_a_wireless_debugging_device_is_not_reported_as_done(fake_adb):
    fake_adb.add("disconnect", returncode=1,
                 stderr=f"error: no such device '{_MDNS}:5555'\n")
    messages = []
    assert _handler(_MDNS, messages).disconnect() is True  # released all the same
    assert not any("Disconnected" in m for m in messages)
    warned = [m for m in messages if m.startswith("[WARNING]")]
    assert len(warned) == 1 and "could not disconnect" in warned[0]
    assert "no such device" in warned[0] and "Wireless debugging" in warned[0]


def test_a_tcp_device_still_disconnects_and_a_usb_one_is_only_released(fake_adb):
    messages = []
    _handler("10.0.0.5:5555", messages).disconnect()
    assert fake_adb.argv_after_adb() == ["disconnect", "10.0.0.5:5555"]
    fake_adb.calls.clear()
    _handler("R58M123ABC", messages).disconnect()
    assert fake_adb.calls == []
