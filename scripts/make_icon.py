#!/usr/bin/env python
"""
Render the TurboADB app icon: an automotive **speedometer** gauge fused with the
**Android robot** + a terminal prompt — drawn supersampled, then downscaled for
crisp edges, and exported as both a PNG and a multi-size Windows ICO.

    python scripts/make_icon.py

Outputs:
    turboadb/assets/icon.png   (1024x1024)
    turboadb/assets/icon.ico   (16..256 multi-size)
"""

from __future__ import annotations

import math
import os
from PIL import Image, ImageDraw

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "turboadb", "assets")

# palette
BG0 = (10, 10, 10, 255)
BG1 = (22, 26, 22, 255)
GREEN = (40, 194, 214, 255)   # cyan-teal gauge start       # android green
GREEN_D = (30, 150, 168, 255)
AMBER = (255, 195, 77, 255)
RED = (255, 94, 94, 255)
DIM = (70, 78, 72, 255)
WHITE = (235, 245, 238, 255)
ROBOT = (226, 234, 240, 255)   # light robot — stands out from the cyan gauge
GLOW = (40, 194, 214)          # cyan glow


def _lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(len(a)))


def _rounded_bg(size):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    r = int(size * 0.22)
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=r, fill=BG0)
    # subtle radial green glow toward centre
    glow = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    cx, cy = size / 2, size * 0.46
    for i in range(28, 0, -1):
        rad = size * 0.5 * i / 28
        a = int(16 * (1 - i / 28))
        gd.ellipse([cx - rad, cy - rad, cx + rad, cy + rad],
                   fill=(GLOW[0], GLOW[1], GLOW[2], a))
    img.alpha_composite(glow)
    # mask the glow to the rounded rect
    mask = Image.new("L", (size, size), 0)
    md = ImageDraw.Draw(mask)
    md.rounded_rectangle([0, 0, size - 1, size - 1], radius=r, fill=255)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(img, (0, 0), mask)
    return out


def _gauge(d, size):
    """A clean speedometer ring (green→amber→red) centred on the canvas."""
    cx, cy = size / 2, size / 2
    radius = size * 0.37
    width = int(size * 0.05)
    start, end = 135, 405          # 270° sweep, gap at the bottom
    steps = 160
    for i in range(steps):
        t0 = i / steps
        a0 = start + (end - start) * t0
        a1 = start + (end - start) * (i + 1) / steps
        if t0 < 0.55:
            col = _lerp(GREEN, AMBER, t0 / 0.55)
        else:
            col = _lerp(AMBER, RED, (t0 - 0.55) / 0.45)
        box = [cx - radius, cy - radius, cx + radius, cy + radius]
        d.arc(box, a0, a1 + 1, fill=col, width=width)
    # tick marks just inside the ring
    for k in range(10):
        ang = math.radians(start + (end - start) * k / 9)
        r1 = radius - width * 0.85
        r2 = radius - width * 1.7
        x1, y1 = cx + r1 * math.cos(ang), cy + r1 * math.sin(ang)
        x2, y2 = cx + r2 * math.cos(ang), cy + r2 * math.sin(ang)
        col = RED if k >= 7 else (AMBER if k >= 5 else DIM)
        d.line([x1, y1, x2, y2], fill=col, width=max(2, int(size * 0.006)))
    return cx, cy, radius


def _android(d, size, cx, cy, radius):
    """A bold, friendly Android robot head centred in the gauge."""
    hr = radius * 0.58
    top = cy - hr * 0.30                 # head sits slightly high; body fills below
    # dome
    d.pieslice([cx - hr, top - hr, cx + hr, top + hr], 180, 360, fill=ROBOT)
    # body (rounded rectangle just under the dome)
    d.rounded_rectangle([cx - hr, top, cx + hr, top + hr * 0.92],
                        radius=int(hr * 0.16), fill=ROBOT)
    # antennae
    aw = max(3, int(size * 0.012))
    for sx in (-0.42, 0.42):
        ax = cx + hr * sx
        ay = top - hr * 0.92
        d.line([ax, ay, ax - sx * hr * 0.30, top - hr * 0.30], fill=ROBOT, width=aw)
    # eyes
    er = hr * 0.13
    ey = top - hr * 0.30
    for sx in (-0.40, 0.40):
        ex = cx + hr * sx
        d.ellipse([ex - er, ey - er, ex + er, ey + er], fill=BG0)
    # needle: a sleek pointer from the centre into the redline (upper right)
    nang = math.radians(135 + 270 * 0.80)
    nx, ny = cx + radius * 0.92 * math.cos(nang), cy + radius * 0.92 * math.sin(nang)
    d.line([cx, cy + hr * 0.2, nx, ny], fill=WHITE, width=max(4, int(size * 0.013)))
    hub = size * 0.02
    d.ellipse([cx - hub, cy + hr * 0.2 - hub, cx + hub, cy + hr * 0.2 + hub], fill=WHITE)


def render(size=1024):
    ss = 2
    S = size * ss
    base = _rounded_bg(S)
    d = ImageDraw.Draw(base)
    cx, cy, radius = _gauge(d, S)
    _android(d, S, cx, cy, radius)
    return base.resize((size, size), Image.LANCZOS)


def main():
    os.makedirs(OUT, exist_ok=True)
    img = render(1024)
    png = os.path.join(OUT, "icon.png")
    img.save(png)
    ico = os.path.join(OUT, "icon.ico")
    img.save(ico, sizes=[(16, 16), (24, 24), (32, 32), (48, 48),
                         (64, 64), (128, 128), (256, 256)])
    print(f"Wrote {png}")
    print(f"Wrote {ico}")


if __name__ == "__main__":
    main()
