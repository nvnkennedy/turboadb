"""The first identity probe after connecting is quick, best effort and quiet:
a slow reply must never surface as an error (the GUI turns ERROR log lines into
a red popup)."""
from turboadb import ADBConfig, ADBHandler, ADBTimeoutError


def _output(*values):
    return "\n".join(values) + "\n"


def test_quick_identity_reads_the_first_banner_fields(fake_adb):
    fake_adb.add(
        "getprop ro.product.manufacturer",
        stdout=_output("vivo", "V2318", "16", "36", "PD2323F", "arm64-v8a"),
    )
    ident = ADBHandler(ADBConfig(serial="x")).quick_identity(timeout=1.5)
    assert ident == {
        "manufacturer": "vivo",
        "model": "V2318",
        "android_version": "16",
        "sdk": "36",
        "device": "PD2323F",
        "abi": "arm64-v8a",
    }


def test_slow_device_gives_empty_identity_without_error_logs(fake_adb, monkeypatch):
    lines = []
    handler = ADBHandler(ADBConfig(serial="x"), safe=True, log_callback=lines.append)

    def slow_run(args, timeout=None, **_kwargs):
        raise ADBTimeoutError(f"adb command timed out after {timeout}s")

    monkeypatch.setattr(handler, "_run", slow_run)
    assert handler.quick_identity(timeout=0.2) == {}
    assert not [line for line in lines if line.startswith(("[ERROR]", "[WARNING]"))]
    assert any(line.startswith("[DEBUG]") for line in lines)


def test_unexpected_output_gives_empty_identity(fake_adb):
    fake_adb.add("getprop ro.product.manufacturer", stdout="\n")
    assert ADBHandler(ADBConfig(serial="x")).quick_identity() == {}


def test_device_details_thread_reports_failure_without_error_logs(monkeypatch):
    import pytest

    pytest.importorskip("PyQt5")
    from turboadb.gui.device_tab import _DeviceInfoThread

    lines = []
    handler = ADBHandler(ADBConfig(serial="x"), safe=True, log_callback=lines.append)

    def boom(*, safe=None):
        raise ADBTimeoutError("getprop timed out")

    monkeypatch.setattr(handler, "device_info", boom)
    results = []
    thread = _DeviceInfoThread(handler)
    thread.done.connect(results.append)
    thread.run()  # synchronously, in this thread
    assert len(results) == 1 and results[0].success is False
    assert "timed out" in str(results[0].error)
    assert not [line for line in lines if line.startswith("[ERROR]")]
