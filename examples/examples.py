"""
TurboADB usage examples. These touch a real device — edit the target, then run
the pieces you need. Nothing here runs on import.
"""

from turboadb import ADBHandler, ADBConfig, ScrcpyOptions, list_devices


def list_attached():
    for d in list_devices():
        print(d)


def usb_basics():
    with ADBHandler() as dev:                      # only attached device
        print(dev.device_info())
        print(dev.shell("getprop ro.build.version.release").text)
        dev.push("README.md", "/sdcard/README.md")
        dev.pull("/sdcard/README.md", "pulled.md")


def network_head_unit():
    cfg = ADBConfig(host="192.168.1.50", port=5555)
    with ADBHandler(cfg) as hu:
        if hu.is_automotive():
            print("Android Automotive OS head unit detected")
        hu.logcat(tag="CarService", match=r"ERROR|FATAL", on_line=print,
                  save_to="car.log", stop_on_match=True)


def install_and_launch():
    with ADBHandler() as dev:
        dev.install("app.apk", grant_perms=True)
        dev.start_app("com.example.app")


def mirror():
    with ADBHandler() as dev:
        sess = dev.mirror(ScrcpyOptions(max_size=1280, bit_rate="8M"))
        input("Mirroring… press Enter to stop ")
        sess.stop()


def safe_mode_for_guis():
    dev = ADBHandler(ADBConfig(serial=None), safe=True)
    res = dev.connect()
    if res:
        print("connected:", res.value)
    else:
        print("error:", res.error)


if __name__ == "__main__":
    list_attached()
