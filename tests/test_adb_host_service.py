"""The adb server's socket protocol is framed in one place.

Every request is its length in four hex digits, then the service.  The length
was written out by hand beside each service string (``b"000chost:version"``,
``b"000ehost:devices-l"``), where a changed string kept its old length and the
server answered ``FAIL``.
"""
import inspect
import socket
import threading

from turboadb import devices, tools


class _Recorder:
    """A socket that records what is sent and answers from a script."""

    def __init__(self, reply=b"OKAY"):
        self.sent = b""
        self.reply = bytearray(reply)

    def sendall(self, data):
        self.sent += data

    def recv(self, size):
        out, self.reply = bytes(self.reply[:size]), self.reply[size:]
        return out


def test_a_request_carries_its_own_length():
    for service in ("host:version", "host:devices-l", "host-serial:R58M123:features"):
        sock = _Recorder()
        assert tools.adb_request(sock, service) is True
        assert sock.sent == b"%04x" % len(service) + service.encode()
    assert tools.adb_request(_Recorder(b"FAIL0004nope"), "host:kill") is False


def test_a_reply_is_read_by_its_length():
    assert tools.adb_reply(_Recorder(b"0005hello and more")) == b"hello"
    assert tools.adb_reply(_Recorder(b"zzzz")) is None
    assert tools.adb_reply(_Recorder(b"0010short")) is None


class _Quiet(_Recorder):
    """A stream that times out once before its next reply (the device tracker's)."""

    def __init__(self, reply):
        super().__init__(reply)
        self.quiet = True

    def recv(self, size):
        if self.quiet:
            self.quiet = False
            raise socket.timeout("nothing yet")
        return super().recv(size)


def test_a_quiet_stream_can_wait_for_its_next_reply():
    assert tools.adb_reply(_Quiet(b"0004R58M"), retry_timeouts=True) == b"R58M"
    try:
        tools.adb_reply(_Quiet(b"0004R58M"))
    except socket.timeout:
        pass
    else:
        raise AssertionError("a one-shot reply must not wait forever")


class _StrictServer:
    """Answers only a request whose length prefix is exactly its length."""

    def __init__(self, replies):
        self.replies = replies
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with conn:
                try:
                    conn.settimeout(0.5)
                    size = int(conn.recv(4), 16)
                    data = b""
                    while len(data) < size:
                        chunk = conn.recv(size - len(data))
                        if not chunk:
                            break
                        data += chunk
                    conn.settimeout(0.05)
                    try:
                        extra = conn.recv(64)  # more than was announced
                    except socket.timeout:
                        extra = b""
                    payload = None if extra else self.replies.get(data.decode())
                    if payload is None:
                        conn.sendall(b"FAIL0007unknown")
                    else:
                        conn.sendall(b"OKAY" + b"%04x" % len(payload) + payload)
                except (OSError, ValueError):
                    pass

    def close(self):
        self.sock.close()


def test_the_probes_and_the_device_list_use_the_framing(monkeypatch):
    server = _StrictServer({
        "host:version": b"0029",
        "host:devices-l": b"R58M123 device product:x model:Pixel_7 transport_id:1\n",
    })
    try:
        monkeypatch.setenv("ANDROID_ADB_SERVER_PORT", str(server.port))
        assert tools.is_adb_server_alive(timeout=1.0)
        assert tools.adb_server_version(timeout=1.0) == 41
        assert tools.adb_query("host:devices-l", timeout=1.0).startswith(b"R58M123")
        assert [d.serial for d in devices._list_devices_socket(timeout=1.0)] == ["R58M123"]
        assert tools.adb_query("host:nonsense", timeout=1.0) is None  # FAIL is None
    finally:
        server.close()
    for fn in (tools.is_adb_server_alive, devices._list_devices_socket):
        assert 'b"00' not in inspect.getsource(fn)  # no length written by hand
