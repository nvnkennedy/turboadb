"""Connection / behaviour configuration objects for the ADB handler and scrcpy."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


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

    serial: Optional[str] = None          # device serial (or "host:port" for TCP)
    host: Optional[str] = None            # for network (TCP/IP) connect
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
    command_timeout: Optional[float] = None   # default per-command timeout (s)
    connect_timeout: float = 20.0             # wait-for-device window on connect
    auto_connect: bool = True                 # run `adb connect` for network targets
    auto_wait: bool = True                    # wait-for-device after connect
    encoding: str = "utf-8"

    def __post_init__(self):
        # A bare "host:port" passed as serial is also a valid network target.
        if self.serial and self.host is None and ":" in self.serial:
            h, _, p = self.serial.rpartition(":")
            if p.isdigit():
                self.host, self.port = h, int(p)
        # Normalise a remote adb-server host given WITH a port (e.g. the user
        # typed "10.232.10.199:5037"): split the port out so we never build a
        # doubled "host:port:port" address that scrcpy/adb rejects with
        # "no host in '…:5037:5037'".
        if self.adb_server_host:
            h = self.adb_server_host.strip()
            while ":" in h:                      # strip ALL ":port" suffixes
                host, _, p = h.rpartition(":")
                if host and p.isdigit():
                    h, self.adb_server_port = host, int(p)
                else:
                    break
            self.adb_server_host = h

    @property
    def target(self) -> Optional[str]:
        """The adb serial to pass with ``-s`` (``host:port`` for network)."""
        if self.host:
            return f"{self.host}:{self.port}"
        return self.serial

    @property
    def is_remote_server(self) -> bool:
        return bool(self.adb_server_host)

    def __repr__(self) -> str:
        srv = (f", adb_server={self.adb_server_host}:{self.adb_server_port}"
               if self.adb_server_host else "")
        return (f"ADBConfig(target={self.target!r}{srv}, "
                f"adb_path={self.adb_path!r}, scrcpy_path={self.scrcpy_path!r})")


@dataclass
class ScrcpyOptions:
    """Options for a scrcpy mirroring/control session. All optional; sensible
    defaults mirror at the device's native size with audio off for low latency."""

    max_size: Optional[int] = None        # --max-size (longest edge in px)
    bit_rate: Optional[str] = None        # --video-bit-rate e.g. "8M"
    max_fps: Optional[int] = None         # --max-fps
    video_codec: Optional[str] = None     # --video-codec h264|h265|av1 (h264 = most
                                          # compatible on automotive/IVI encoders)
    render_driver: Optional[str] = None   # --render-driver (e.g. "software" — the
                                          # reliable choice over Remote Desktop /
                                          # GPU-less sessions where d3d/opengl fail)
    crop: Optional[str] = None            # --crop WxH:X:Y (great for IVI displays)
    display_id: Optional[int] = None      # --display-id (multi-display head units)
    record: Optional[str] = None          # --record FILE (mp4/mkv)
    record_format: Optional[str] = None   # --record-format mp4|mkv
    stay_awake: bool = True               # --stay-awake
    turn_screen_off: bool = False         # --turn-screen-off
    show_touches: bool = False            # --show-touches
    fullscreen: bool = False              # --fullscreen
    always_on_top: bool = False           # --always-on-top
    window_borderless: bool = False       # --window-borderless (for GUI embedding)
    window_x: Optional[int] = None        # --window-x
    window_y: Optional[int] = None        # --window-y
    no_audio: bool = False                # audio ON by default (scrcpy 2.0+,
                                          # Android 11+); falls back to video-only
                                          # automatically on devices without it
    no_control: bool = False              # --no-control (view only)
    keyboard_mode: Optional[str] = None   # --keyboard sdk|uhid|aoa — "uhid" is a
                                          # virtual HARDWARE keyboard, which types
                                          # where SDK key-injection is blocked
                                          # (common over RDP / on IVIs)
    force_adb_forward: bool = False       # --force-adb-forward: use a FORWARD
                                          # tunnel instead of reverse — needed on
                                          # head units/IVIs that block adb reverse
    window_title: Optional[str] = None    # --window-title
    extra_args: list = field(default_factory=list)   # any raw extra flags

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
        if self.record:
            args += ["--record", str(self.record)]
        if self.record_format:
            args += ["--record-format", str(self.record_format)]
        if self.stay_awake:
            args += ["--stay-awake"]
        if self.turn_screen_off:
            args += ["--turn-screen-off"]
        if self.show_touches:
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
        if self.no_control:
            args += ["--no-control"]
        if self.keyboard_mode:
            args += [f"--keyboard={self.keyboard_mode}"]
        if self.force_adb_forward:
            args += ["--force-adb-forward"]
        if self.window_title:
            args += ["--window-title", str(self.window_title)]
        args += list(self.extra_args or [])
        return args
