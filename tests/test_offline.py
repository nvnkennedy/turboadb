"""Offline tests — no device, no adb required. Run: python tests/test_offline.py"""

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import logging
logging.disable(logging.CRITICAL)

import turboadb as T
from turboadb import (ADBHandler, ADBConfig, ScrcpyOptions, Device,
                      CommandResult, TransferResult, StreamResult,
                      OperationResult, ADBError, strip_ansi)
from turboadb.devices import _parse_line

print("version", T.__version__)
assert T.__version__

# ---- CommandResult ----
ok = CommandResult("getprop x", 0, "value\n", "", 0.1, device="emu")
assert ok.ok and bool(ok) and ok.text == "value"
bad = CommandResult("x", 1, "", "boom", 0.2)
assert not bad.ok and not bool(bad)
multi = CommandResult("c", 0, "a\n\nb\n c \n", "", 0.0)
assert multi.lines == ["a", "b", " c "], multi.lines
print("CommandResult OK")

# ---- TransferResult math ----
tr = TransferResult("a", "b", "push", 1048576, 2.0, 1)
assert abs(tr.speed_bps - 524288) < 1 and tr.human_size == "1.0MB"
assert "Pushed" in str(tr)
print("TransferResult:", tr.human_speed, tr.human_size)

# ---- StreamResult ----
sr = StreamResult(10, ["FATAL ..."], "log.txt")
assert sr.matched and bool(sr) and sr.lines == 10
print("StreamResult OK")

# ---- ADBConfig target parsing ----
c1 = ADBConfig(serial="emulator-5554")
assert c1.target == "emulator-5554" and c1.host is None
c2 = ADBConfig(host="10.0.0.9")
assert c2.target == "10.0.0.9:5555"
c3 = ADBConfig(serial="192.168.1.7:5555")     # bare host:port as serial
assert c3.host == "192.168.1.7" and c3.port == 5555 and c3.target == "192.168.1.7:5555"
print("ADBConfig target:", c1.target, "|", c2.target, "|", c3.target)

# ---- ScrcpyOptions ----
args = ScrcpyOptions(max_size=1024, bit_rate="8M", record="d.mp4",
                     turn_screen_off=True, no_audio=True).to_args()
for flag in ("--max-size", "1024", "--video-bit-rate", "8M", "--record",
             "--turn-screen-off", "--no-audio"):
    assert flag in args, flag
codec = ScrcpyOptions(video_codec="h264", display_id=2).to_args()
assert "--video-codec" in codec and "h264" in codec
assert "--display-id" in codec and "2" in codec
print("ScrcpyOptions args OK")

# ---- scrcpy --list-displays parser (no device needed) ----
import re as _re
_sample = ("[server] INFO: List of displays:\n"
           "    --display-id=0    (1920x1080)\n"
           "    --display-id=2    (1280x720)\n")
_ids = [int(m.group(1)) for m in _re.finditer(
    r"--display(?:-id)?[= ](\d+)", _sample)]
assert _ids == [0, 2], _ids
print("display-list parse OK")

# ---- auto-fetch env toggle ----
import os as _os
from turboadb import toolsdl
_old = _os.environ.get("TURBOADB_AUTO_FETCH")
_os.environ["TURBOADB_AUTO_FETCH"] = "0"
assert toolsdl.auto_fetch_enabled() is False
_os.environ["TURBOADB_AUTO_FETCH"] = "1"
assert toolsdl.auto_fetch_enabled() is True
_os.environ.pop("TURBOADB_AUTO_FETCH", None)
assert toolsdl.auto_fetch_enabled() is True          # default ON
if _old is not None:
    _os.environ["TURBOADB_AUTO_FETCH"] = _old
assert callable(toolsdl.fetch_tools) and callable(toolsdl.ensure_tools)
print("auto-fetch toggle OK")

# ---- upgrade decision logic (only upgrade when newer / missing) ----
assert toolsdl._decide("37.0.0", "37.0.0") is False    # up to date
assert toolsdl._decide("36.0.0", "37.0.0") is True     # newer available
assert toolsdl._decide(None, "37.0.0") is True         # not installed
assert toolsdl._decide("37.0.0", None) is None         # can't determine
assert callable(toolsdl.check_updates) and callable(toolsdl.upgrade_tools)
print("upgrade decision OK")

# ---- ScrcpyOptions embedding flags ----
emb = ScrcpyOptions(window_borderless=True, window_x=0, window_y=0).to_args()
assert "--window-borderless" in emb and "--window-x" in emb
print("embed options OK")

# ---- device line parsing ----
d = _parse_line("emulator-5554  device product:sdk model:Pixel_6 "
                "device:emu transport_id:3")
assert isinstance(d, Device) and d.serial == "emulator-5554"
assert d.is_online and d.model == "Pixel_6" and not d.is_network
net = _parse_line("192.168.1.50:5555  device model:HeadUnit")
assert net.is_network and "net" in net.label
assert _parse_line("List of devices attached") is None
assert _parse_line("* daemon started successfully") is None
print("device parse OK")

# ---- strip_ansi ----
assert strip_ansi("\x1b[32mgreen\x1b[0m\r\n") == "green\n"
print("strip_ansi OK")

# ---- safe-mode vs raise-mode guard ----
def boom():
    raise RuntimeError("kaboom")

h_safe = ADBHandler(ADBConfig(serial="x"), safe=True)
r = h_safe._guard("op", boom)
assert isinstance(r, OperationResult) and not r and isinstance(r.error, RuntimeError)
assert r.action == "op"
print("safe-mode wrap OK:", type(r.error).__name__)

h_raise = ADBHandler(ADBConfig(serial="x"))
try:
    h_raise._guard("op", boom)
    raise SystemExit("FAIL: raise mode should propagate")
except RuntimeError:
    print("raise-mode propagation OK")

# per-call override: safe=False forces raise even on a safe-default handler
try:
    h_safe._guard("op", boom, safe=False)
    raise SystemExit("FAIL: per-call safe=False should raise")
except RuntimeError:
    print("per-call safe override OK")

# ---- ForwardHandle repr (no adb call) ----
fh = T.ForwardHandle(h_safe, "forward", "tcp:9222", "localabstract:x")
assert "tcp:9222" in repr(fh) and "->" in repr(fh)
print("ForwardHandle OK")

# ---- exception hierarchy ----
from turboadb.exceptions import (ADBConnectionError, ADBCommandError,
                                 ADBTransferError, ScrcpyError, ADBNotFoundError)
for exc in (ADBConnectionError, ADBCommandError, ADBTransferError, ScrcpyError,
            ADBNotFoundError):
    assert issubclass(exc, ADBError)
ce = ADBCommandError("cmd", CommandResult("cmd", 1, "", "err", 0.0))
assert "exit=1" in str(ce)
print("exception hierarchy OK")

# ---- repr never crashes ----
assert "ADBHandler" in repr(h_safe)

print("ALL OFFLINE CHECKS PASSED")
