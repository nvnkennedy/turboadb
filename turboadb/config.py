"""Connection / behaviour configuration objects for the ADB handler and scrcpy."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


def validate_port(value, name: str = "port") -> int:
    """Return a valid TCP port or raise a clear :class:`ValueError`.

    Type annotations do not protect public library entry points.  Converting and
    validating ports at the boundary also prevents values from being interpolated
    into command lines by callers of the deployment/startup helpers.
    """
    if isinstance(value, bool):
        raise ValueError(f"{name} must be an integer between 1 and 65535")
    try:
        port = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer between 1 and 65535") from exc
    if not 1 <= port <= 65535:
        raise ValueError(f"{name} must be between 1 and 65535")
    return port


def format_host_port(host: str, port: int) -> str:
    """Format an ADB endpoint, preserving brackets required for IPv6 literals."""
    host = (host or "").strip()
    if not host:
        raise ValueError("host must not be empty")
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    if ":" in host:
        host = f"[{host}]"
    return f"{host}:{validate_port(port)}"


def parse_host_port(value, default_port: Optional[int] = None):
    """Split an endpoint into ``(host, port)`` — the one shared parser.

    Accepts ``host:port``, ``[ipv6]:port``, ``[ipv6]``, a bare host and a bare
    IPv6 literal. Brackets are removed from the host. The port is returned as an
    ``int`` only when it is present and numeric; otherwise *default_port* is
    returned in its place (range validation is left to :func:`validate_port`).
    A bare IPv6 literal such as ``fe80::1`` is never split into host and port.
    """
    text = str(value or "").strip()
    if text.startswith("[") and "]" in text:
        end = text.index("]")
        host, rest = text[1:end], text[end + 1 :]
        if rest.startswith(":") and rest[1:].isdigit():
            return host, int(rest[1:])
        return host, default_port
    if text.count(":") == 1:
        host, _, port = text.partition(":")
        if host and port.isdigit():
            return host, int(port)
    return text, default_port


@dataclass
class ADBConfig:
    """
    Everything needed to select and drive an Android device through adb.

    Two ways to reach a device
    --------------------------
    * **USB**     - leave ``host`` unset. ``serial`` picks a specific device when
                    several are attached (``adb devices``); leave it ``None`` to
                    use the only connected device.
    * **Network** - set ``host`` (and optionally ``port``, default 5555). On
                    :meth:`connect`, the handler runs ``adb connect host:port``
                    and uses ``host:port`` as the device serial. Great for
                    Android Automotive head units / IVI on the bench LAN.

    >>> ADBConfig(serial="emulator-5554")          # a specific USB device
    >>> ADBConfig(host="192.168.1.50", port=5555)  # a head unit over Wi-Fi/Ethernet
    """

    serial: Optional[str] = None  # device serial (or "host:port" for TCP)
    host: Optional[str] = None  # for network (TCP/IP) connect
    port: int = 5555

    # --- remote ADB server (drive a device plugged into ANOTHER machine) ---
    # Set adb_server_host to that machine's IP and every adb command runs through
    # its adb server (``adb -H host -P port``), exactly like being sat at it. On
    # that machine, expose the server once with:  adb -a nodaemon server start
    adb_server_host: Optional[str] = None
    adb_server_port: int = 5037

    # --- tool locations (auto-detected on PATH / SDK / bundled if unset) ---
    adb_path: Optional[str] = None
    scrcpy_path: Optional[str] = None

    # --- behaviour ---
    command_timeout: Optional[float] = 60.0  # default per-command timeout (s)
    transfer_timeout: Optional[float] = 600.0  # default push/pull timeout (s)
    connect_timeout: float = 20.0  # wait-for-device window on connect
    auto_connect: bool = True  # run `adb connect` for network targets
    auto_wait: bool = True  # wait-for-device after connect
    encoding: str = "utf-8"

    def __post_init__(self):
        self.port = validate_port(self.port)
        self.adb_server_port = validate_port(self.adb_server_port, "adb_server_port")
        if self.command_timeout is not None and self.command_timeout <= 0:
            raise ValueError("command_timeout must be positive or None")
        if self.transfer_timeout is not None and self.transfer_timeout <= 0:
            raise ValueError("transfer_timeout must be positive or None")
        # A bare "host:port" passed as serial is also a valid network target.
        if self.serial and self.host is None and ":" in self.serial:
            h, p = parse_host_port(self.serial)
            if h and p is not None:
                self.host, self.port = h, p
        # Normalise a remote adb-server host given WITH a port (e.g. the user
        # typed "10.232.10.199:5037" or "[::1]:5037"): split the port out so we never build a
        # doubled "host:port:port" address that scrcpy/adb rejects with
        # "no host in '…:5037:5037'". Surrounding whitespace is always removed.
        if self.adb_server_host is not None:
            h, p = parse_host_port(self.adb_server_host)
            self.adb_server_host = h or None
            if h and p is not None:
                self.adb_server_port = p
        # Serial/server endpoint parsing above can replace the initial values.
        # Validate once more so a value embedded in "host:port" gets the same
        # boundary checks as an explicit argument.
        self.port = validate_port(self.port)
        self.adb_server_port = validate_port(self.adb_server_port, "adb_server_port")

    @property
    def target(self) -> Optional[str]:
        """The adb serial to pass with ``-s`` (``host:port`` for network)."""
        if self.host:
            return format_host_port(self.host, self.port)
        return self.serial

    @property
    def is_remote_server(self) -> bool:
        return bool(self.adb_server_host)

    def __repr__(self) -> str:
        srv = (
            f", adb_server={format_host_port(self.adb_server_host, self.adb_server_port)}"
            if self.adb_server_host
            else ""
        )
        return (
            f"ADBConfig(target={self.target!r}{srv}, "
            f"adb_path={self.adb_path!r}, scrcpy_path={self.scrcpy_path!r})"
        )


@dataclass
class ScrcpyOptions:
    """Options for a scrcpy mirroring/control session. All optional; sensible
    defaults mirror at the device's native size with scrcpy's audio forwarding
    enabled when the device supports it."""

    max_size: Optional[int] = None  # --max-size (longest edge in px)
    bit_rate: Optional[str] = None  # --video-bit-rate e.g. "8M"
    max_fps: Optional[int] = None  # --max-fps
    video_codec: Optional[str] = None  # --video-codec h264|h265|av1 (h264 = most
    # compatible on automotive/IVI encoders)
    render_driver: Optional[str] = None  # --render-driver (e.g. "software" — the
    # reliable choice over Remote Desktop /
    # GPU-less sessions where d3d/opengl fail)
    crop: Optional[str] = None  # --crop WxH:X:Y (great for IVI displays)
    display_id: Optional[int] = None  # --display-id (multi-display head units)
    video_source: Optional[str] = None  # --video-source display|camera (scrcpy 2.2+)
    camera_facing: Optional[str] = None  # --camera-facing front|back|external
    camera_size: Optional[str] = None  # --camera-size WxH
    record: Optional[str] = None  # --record FILE (mp4/mkv)
    record_format: Optional[str] = None  # --record-format mp4|mkv
    stay_awake: bool = True  # --stay-awake
    turn_screen_off: bool = False  # --turn-screen-off
    show_touches: bool = False  # --show-touches
    fullscreen: bool = False  # --fullscreen
    always_on_top: bool = False  # --always-on-top
    window_borderless: bool = False  # --window-borderless (for GUI embedding)
    window_x: Optional[int] = None  # --window-x
    window_y: Optional[int] = None  # --window-y
    # Audio is enabled by default.  These values map to scrcpy 4.x audio
    # controls and are deliberately optional so older scrcpy installations
    # retain their own compatible defaults.
    no_audio: bool = False
    audio_source: Optional[str] = None  # output|playback|mic|voice-call-downlink|voice-performance
    audio_codec: Optional[str] = None  # opus|aac|flac|raw
    audio_bit_rate: Optional[str] = None  # e.g. 128K
    audio_buffer: Optional[int] = None  # capture buffer latency in milliseconds
    audio_output_buffer: Optional[int] = None  # local playback buffer in milliseconds
    audio_dup: bool = False  # keep audio playing on device (playback source only)
    no_control: bool = False  # --no-control (view only)
    keyboard_mode: Optional[str] = None  # --keyboard sdk|uhid|aoa — "uhid" is a
    # virtual HARDWARE keyboard, which types
    # where SDK key-injection is blocked
    # (common over RDP / on IVIs)
    force_adb_forward: bool = False  # --force-adb-forward: use a FORWARD
    # tunnel instead of reverse — needed on
    # head units/IVIs that block adb reverse
    window_title: Optional[str] = None  # --window-title
    no_playback: bool = False  # --no-playback (for headless recording)
    extra_args: list = field(default_factory=list)  # any raw extra flags


    def to_args(self) -> list:
        """Translate the options into a scrcpy argv list."""
        args: list = []
        if self.max_size:
            args += ["--max-size", str(self.max_size)]
        if self.bit_rate:
            args += ["--video-bit-rate", str(self.bit_rate)]
        if self.max_fps:
            args += ["--max-fps", str(self.max_fps)]
        if self.video_codec:
            args += ["--video-codec", str(self.video_codec)]
        if self.render_driver:
            # scrcpy wants the "=" form here (--render-driver=NAME); the
            # space-separated form is silently ignored ("WARN: Could not set
            # render driver"), which is fatal for a GPU-less RDP session
            args += [f"--render-driver={self.render_driver}"]
        if self.crop:
            args += ["--crop", str(self.crop)]
        if self.display_id is not None:
            args += ["--display-id", str(self.display_id)]
        if self.video_source:
            args += [f"--video-source={self.video_source}"]
        if self.camera_facing:
            args += [f"--camera-facing={self.camera_facing}"]
        if self.camera_size:
            args += ["--camera-size", str(self.camera_size)]
        if self.record:
            args += ["--record", str(self.record)]
        if self.record_format:
            args += ["--record-format", str(self.record_format)]
        # scrcpy refuses to start when these are combined with --no-control
        # ("Cannot request to stay awake if control is disabled"), and
        # stay_awake defaults to True — so a view-only session must drop them.
        if self.stay_awake and not self.no_control:
            args += ["--stay-awake"]
        if self.turn_screen_off and not self.no_control:
            args += ["--turn-screen-off"]
        if self.show_touches and not self.no_control:
            args += ["--show-touches"]
        if self.fullscreen:
            args += ["--fullscreen"]
        if self.always_on_top:
            args += ["--always-on-top"]
        if self.window_borderless:
            args += ["--window-borderless"]
        if self.window_x is not None:
            args += ["--window-x", str(self.window_x)]
        if self.window_y is not None:
            args += ["--window-y", str(self.window_y)]
        if self.no_audio:
            args += ["--no-audio"]
        else:
            if self.audio_source:
                args += [f"--audio-source={self.audio_source}"]
            if self.audio_codec:
                args += ["--audio-codec", str(self.audio_codec)]
            if self.audio_bit_rate:
                args += ["--audio-bit-rate", str(self.audio_bit_rate)]
            if self.audio_buffer is not None:
                args += ["--audio-buffer", str(self.audio_buffer)]
            if self.audio_output_buffer is not None:
                args += ["--audio-output-buffer", str(self.audio_output_buffer)]
            if self.audio_dup:
                args += ["--audio-dup"]
        if self.no_control:
            args += ["--no-control"]
        if self.keyboard_mode:
            args += [f"--keyboard={self.keyboard_mode}"]
        if self.force_adb_forward:
            args += ["--force-adb-forward"]
        if self.window_title:
            args += ["--window-title", str(self.window_title)]
        if self.no_playback:
            args += ["--no-playback"]
        args += list(self.extra_args or [])
        return args
