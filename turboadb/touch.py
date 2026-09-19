"""Raw touch input helpers, shared by the engine and its tests.

``ADBHandler.tap_burst`` taps a point thousands of times. One ``adb shell input
tap`` per tap would spend most of its time starting processes (an adb child on
the PC, then a JVM on the device), so the burst runs as ONE device-side loop,
and — when the device lets the shell write to its touchscreen — sends the touch
events themselves with ``sendevent`` instead of ``input``.

Everything here is pure text/number work: parsing ``getevent -pl``, choosing the
touchscreen, scaling PC-side pixels into the device's event units and building
the shell loop. No adb runs from this module.
"""

from __future__ import annotations

from typing import List, NamedTuple, Optional, Sequence, Tuple

# Linux input event types and codes (see include/uapi/linux/input-event-codes.h)
EV_SYN, EV_KEY, EV_ABS = 0, 1, 3
SYN_REPORT, SYN_MT_REPORT = 0, 2
BTN_TOUCH = 330
ABS_MT_SLOT = 0x2F
ABS_MT_POSITION_X, ABS_MT_POSITION_Y = 0x35, 0x36
ABS_MT_TRACKING_ID = 0x39

_AXIS_CODES = {"ABS_MT_POSITION_X": "max_x", "ABS_MT_POSITION_Y": "max_y"}


class TouchDevice(NamedTuple):
    """A touchscreen as ``getevent -pl`` describes it."""

    path: str
    name: str
    max_x: int
    max_y: int
    protocol: str  # "b" (slots and tracking ids) or "a" (SYN_MT_REPORT)
    direct: bool  # INPUT_PROP_DIRECT: a screen, not a touchpad

    def as_dict(self) -> dict:
        return dict(self._asdict())


def parse_touch_devices(text: str) -> List[TouchDevice]:
    """Every touchscreen in ``getevent -pl`` output, in the order listed."""
    devices: List[TouchDevice] = []
    path = name = ""
    axes = {"max_x": 0, "max_y": 0}
    protocol = ""
    direct = False

    def flush():
        if path and axes["max_x"] and axes["max_y"]:
            devices.append(TouchDevice(path, name, axes["max_x"], axes["max_y"],
                                       protocol or "a", direct))

    for raw in (text or "").splitlines():
        line = raw.strip()
        if line.startswith("add device"):
            flush()
            path = line.split(":", 1)[1].strip() if ":" in line else ""
            name, protocol, direct = "", "", False
            axes = {"max_x": 0, "max_y": 0}
            continue
        if not path:
            continue
        if line.startswith("name:"):
            name = line.split(":", 1)[1].strip().strip('"')
            continue
        if "INPUT_PROP_DIRECT" in line:
            direct = True
            continue
        for code, key in _AXIS_CODES.items():
            if code in line:
                axes[key] = max(axes[key], _axis_max(line))
        if "ABS_MT_TRACKING_ID" in line or "ABS_MT_SLOT" in line:
            protocol = "b"
    flush()
    return devices


def _axis_max(line: str) -> int:
    """The ``max`` of an axis line, e.g. ``… : value 0, min 0, max 1079, …``."""
    for part in line.split(","):
        part = part.strip()
        if part.startswith("max "):
            try:
                return int(part[4:].strip())
            except ValueError:
                return 0
    return 0


def pick_touch_device(devices: Sequence[TouchDevice]) -> Optional[TouchDevice]:
    """The device to tap on: a real screen before a touchpad, slots before the
    older protocol, then the largest one (the main display on a car with two)."""
    if not devices:
        return None
    return sorted(
        devices,
        key=lambda d: (not d.direct, d.protocol != "b", -(d.max_x * d.max_y)),
    )[0]


def scale_point(x: int, y: int, screen: Tuple[int, int], device: TouchDevice) -> Tuple[int, int]:
    """Turn a point in display pixels into the touchscreen's own units.

    Most panels report exactly the display's resolution, but a digitiser with
    its own (often larger) grid needs the point scaled, or every tap would land
    near the top-left corner."""
    width, height = (int(screen[0]), int(screen[1])) if screen else (0, 0)
    ev_x = round(x * (device.max_x + 1) / width) if width > 0 else int(x)
    ev_y = round(y * (device.max_y + 1) / height) if height > 0 else int(y)
    return (max(0, min(device.max_x, int(ev_x))), max(0, min(device.max_y, int(ev_y))))


def tap_events(device: TouchDevice, x: int, y: int, *, tracking_id: int = 1):
    """The (type, code, value) events of one complete tap at *x*, *y*."""
    if device.protocol == "b":
        return [
            (EV_ABS, ABS_MT_TRACKING_ID, tracking_id),
            (EV_ABS, ABS_MT_POSITION_X, x),
            (EV_ABS, ABS_MT_POSITION_Y, y),
            (EV_KEY, BTN_TOUCH, 1),
            (EV_SYN, SYN_REPORT, 0),
            (EV_ABS, ABS_MT_TRACKING_ID, -1),
            (EV_KEY, BTN_TOUCH, 0),
            (EV_SYN, SYN_REPORT, 0),
        ]
    return [
        (EV_ABS, ABS_MT_POSITION_X, x),
        (EV_ABS, ABS_MT_POSITION_Y, y),
        (EV_KEY, BTN_TOUCH, 1),
        (EV_SYN, SYN_MT_REPORT, 0),
        (EV_SYN, SYN_REPORT, 0),
        (EV_KEY, BTN_TOUCH, 0),
        (EV_SYN, SYN_MT_REPORT, 0),
        (EV_SYN, SYN_REPORT, 0),
    ]


def sendevent_tap(device: TouchDevice, x: int, y: int) -> str:
    """One tap as ``sendevent`` calls.

    ``sendevent`` is the device's own tool, so the events are packed the way
    that kernel expects — writing raw structs from the shell would have to
    guess between the 32- and 64-bit layouts."""
    # && so a refused event ends the tap instead of sending half of one
    return " && ".join(
        f"sendevent {device.path} {type_} {code} {value}"
        for type_, code, value in tap_events(device, x, y)
    )


def input_tap(x: int, y: int, display_flag: Sequence[str] = ()) -> str:
    """One tap through the device's ``input`` tool (works on any device)."""
    return " ".join(["input", *display_flag, "tap", str(int(x)), str(int(y))])


def burst_script(tap_command: str, count: int, *, sleep_s: float = 0.0,
                 progress_every: int = 0, marker: str = "@@") -> str:
    """A device-side loop that runs *tap_command* *count* times.

    The loop runs on the device, so no tap waits for the PC. It prints
    ``<marker><taps so far>`` every *progress_every* taps (and once at the end),
    which is how the caller follows a long burst and knows how many taps the
    device really made."""
    count = max(1, int(count))
    # A tap the device refuses (no permission on the input node, a bad display
    # id) ends the burst there and then: running the whole loop out would report
    # thousands of taps that never happened.
    steps = [f'{tap_command} || {{ echo "tap failed"; exit 3; }}', "i=$((i+1))"]
    if progress_every > 0:
        steps.append(f'[ $((i % {int(progress_every)})) -eq 0 ] && echo "{marker}$i"')
    if sleep_s > 0:
        steps.append(f"sleep {sleep_s:g}")
    body = "; ".join(steps)
    return f'i=0; while [ $i -lt {count} ]; do {body}; done; echo "{marker}$i"'


def parse_progress(line: str, marker: str = "@@") -> Optional[int]:
    """``"@@250"`` -> 250; None for any other line the device printed."""
    text = line.strip()
    if not text.startswith(marker):
        return None
    try:
        return int(text[len(marker):])
    except ValueError:
        return None
