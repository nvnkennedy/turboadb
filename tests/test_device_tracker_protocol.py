"""The main window's device tracker speaks the adb server's socket protocol
through the one framing in ``tools`` (``adb_request`` / ``adb_reply``).

It framed ``host:track-devices-l`` and read the replies by hand, a copy of
what those helpers do.  A fake adb server on a free local port answers here,
with every reply split across several TCP packets.
"""
import socket
import threading
import time

import pytest

pytest.importorskip("PyQt5")

import turboadb.gui.main_window as mw  # noqa: E402
from turboadb import tools  # noqa: E402


def _fake_server(replies, status=b"OKAY"):
    """One connection: record the request, answer *status*, then *replies*."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    asked = []

    def serve():
        try:
            conn, _ = srv.accept()
        except OSError:
            return
        try:
            with conn:
                size = int(conn.recv(4), 16)
                asked.append(conn.recv(size).decode("ascii"))
                conn.sendall(status)
                for payload in replies if status == b"OKAY" else ():
                    framed = b"%04x" % len(payload) + payload
                    for i in range(0, len(framed), 3):
                        conn.sendall(framed[i:i + 3])
                        time.sleep(0.005)
                time.sleep(0.5)
        except OSError:
            pass  # the tracker hung up (stopped)
        finally:
            srv.close()

    threading.Thread(target=serve, daemon=True).start()
    return srv.getsockname()[1], asked


def _track(qapp, monkeypatch, port, until):
    monkeypatch.setattr(tools, "local_adb_port", lambda *_a: port)
    framed = []
    real = tools.adb_request
    monkeypatch.setattr(tools, "adb_request",
                        lambda sock, service: framed.append(service) or real(sock, service))
    tracker = mw._DeviceTracker()
    reports = []
    tracker.result.connect(reports.append)
    tracker.start()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not until(reports):
        qapp.processEvents()
        time.sleep(0.01)
    tracker.stop()
    assert tracker.wait(5000)
    qapp.processEvents()
    return reports, framed


def test_the_tracker_reads_every_device_change(qapp, monkeypatch):
    port, asked = _fake_server([
        b"R58M1\tdevice product:a model:Pixel_7 device:a transport_id:1\n",
        b"R58M1\tdevice product:a model:Pixel_7 device:a transport_id:1\n"
        b"10.0.0.5:5555\tunauthorized transport_id:2\n",
        b"",  # every device gone
    ])
    reports, framed = _track(qapp, monkeypatch, port, lambda r: len(r) >= 3)
    assert asked == ["host:track-devices-l"] and framed == ["host:track-devices-l"]
    assert [[(d.serial, d.state) for d in report] for report in reports[:3]] == [
        [("R58M1", "device")],
        [("R58M1", "device"), ("10.0.0.5:5555", "unauthorized")],
        [],
    ]
    assert reports[0][0].model == "Pixel_7"


def test_a_refused_request_reports_nothing(qapp, monkeypatch):
    port, asked = _fake_server([b"R58M1\tdevice\n"], status=b"FAIL")
    reports, _framed = _track(qapp, monkeypatch, port, lambda r: False if not asked else True)
    assert asked == ["host:track-devices-l"] and reports == []
