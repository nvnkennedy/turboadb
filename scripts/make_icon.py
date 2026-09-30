#!/usr/bin/env python
"""
Render the TurboADB app icon: an automotive **speedometer** gauge fused with the
**Android robot** + a terminal prompt — drawn supersampled, then downscaled for
crisp edges, and exported as both a PNG and a multi-size Windows ICO.

    python scripts/make_icon.py

Two variants, for Windows' dark and light taskbars (the app shows the one
that matches):

    turboadb/assets/icon.png         (1024x1024, dark: the default)
    turboadb/assets/icon.ico         (16..256 multi-size)
    turboadb/assets/icon-light.png   (light)
    turboadb/assets/icon-light.ico
"""

from __future__ import annotations

import math
import os
from PIL import Image, ImageDraw

OUT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "turboadb", "assets"
)

# tile, its glow and edge, the gauge (green→amber→red), dim ticks, robot, needle
PALETTES = {
    "dark": {
        "bg": (10, 10, 10, 255), "glow": (40, 194, 214), "glow_alpha": 16, "edge": None,
        "green": (40, 194, 214, 255), "amber": (255, 195, 77, 255), "red": (255, 94, 94, 255),
        "dim": (70, 78, 72, 255), "robot": (226, 234, 240, 255), "needle": (235, 245, 238, 255),
    },
    # a light tile needs a hairline edge on a light taskbar, a dark robot, and
    # deeper gauge colours to hold their contrast on white
    "light": {
        "bg": (246, 248, 250, 255), "glow": (40, 194, 214), "glow_alpha": 24,
        "edge": (196, 204, 212, 255),
        "green": (16, 156, 178, 255), "amber": (234, 152, 20, 255), "red": (224, 66, 66, 255),
        "dim": (170, 178, 186, 255), "robot": (38, 50, 64, 255), "needle": (22, 30, 40, 255),
    },
}
SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]


def _lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(len(a)))


def _rounded_bg(size, pal):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    r = int(size * 0.22)
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=r, fill=pal["bg"])
    # subtle radial glow toward centre
    glow = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    cx, cy = size / 2, size * 0.46
    g = pal["glow"]
    for i in range(28, 0, -1):
        rad = size * 0.5 * i / 28
        a = int(pal["glow_alpha"] * (1 - i / 28))
        gd.ellipse([cx - rad, cy - rad, cx + rad, cy + rad], fill=(g[0], g[1], g[2], a))
    img.alpha_composite(glow)
    if pal["edge"]:
        w = max(2, int(size * 0.012))
        d = ImageDraw.Draw(img)
        d.rounded_rectangle([w // 2, w // 2, size - 1 - w // 2, size - 1 - w // 2],
                            radius=r, outline=pal["edge"], width=w)
    # mask the glow to the rounded rect
    mask = Image.new("L", (size, size), 0)
    md = ImageDraw.Draw(mask)
    md.rounded_rectangle([0, 0, size - 1, size - 1], radius=r, fill=255)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(img, (0, 0), mask)
    return out


def _gauge(d, size, pal):
    """A clean speedometer ring (green→amber→red) centred on the canvas."""
    cx, cy = size / 2, size / 2
    radius = size * 0.37
    width = int(size * 0.05)
    start, end = 135, 405  # 270° sweep, gap at the bottom
    steps = 160
    for i in range(steps):
        t0 = i / steps
        a0 = start + (end - start) * t0
        a1 = start + (end - start) * (i + 1) / steps
        if t0 < 0.55:
            col = _lerp(pal["green"], pal["amber"], t0 / 0.55)
        else:
            col = _lerp(pal["amber"], pal["red"], (t0 - 0.55) / 0.45)
        box = [cx - radius, cy - radius, cx + radius, cy + radius]
        d.arc(box, a0, a1 + 1, fill=col, width=width)
    # tick marks just inside the ring
    for k in range(10):
        ang = math.radians(start + (end - start) * k / 9)
        r1 = radius - width * 0.85
        r2 = radius - width * 1.7
        x1, y1 = cx + r1 * math.cos(ang), cy + r1 * math.sin(ang)
        x2, y2 = cx + r2 * math.cos(ang), cy + r2 * math.sin(ang)
        col = pal["red"] if k >= 7 else (pal["amber"] if k >= 5 else pal["dim"])
        d.line([x1, y1, x2, y2], fill=col, width=max(2, int(size * 0.006)))
    return cx, cy, radius


def _android(d, size, cx, cy, radius, pal):
    """A bold, friendly Android robot head centred in the gauge."""
    robot, needle = pal["robot"], pal["needle"]
    hr = radius * 0.58
    top = cy - hr * 0.30  # head sits slightly high; body fills below
    # dome
    d.pieslice([cx - hr, top - hr, cx + hr, top + hr], 180, 360, fill=robot)
    # body (rounded rectangle just under the dome)
    d.rounded_rectangle([cx - hr, top, cx + hr, top + hr * 0.92], radius=int(hr * 0.16), fill=robot)
    # antennae
    aw = max(3, int(size * 0.012))
    for sx in (-0.42, 0.42):
        ax = cx + hr * sx
        ay = top - hr * 0.92
        d.line([ax, ay, ax - sx * hr * 0.30, top - hr * 0.30], fill=robot, width=aw)
    # eyes
    er = hr * 0.13
    ey = top - hr * 0.30
    for sx in (-0.40, 0.40):
        ex = cx + hr * sx
        d.ellipse([ex - er, ey - er, ex + er, ey + er], fill=pal["bg"])
    # needle: a sleek pointer from the centre into the redline (upper right)
    nang = math.radians(135 + 270 * 0.80)
    nx, ny = cx + radius * 0.92 * math.cos(nang), cy + radius * 0.92 * math.sin(nang)
    d.line([cx, cy + hr * 0.2, nx, ny], fill=needle, width=max(4, int(size * 0.013)))
    hub = size * 0.02
    d.ellipse([cx - hub, cy + hr * 0.2 - hub, cx + hub, cy + hr * 0.2 + hub], fill=needle)


def render(size=1024, variant="dark"):
    pal = PALETTES[variant]
    ss = 2
    S = size * ss
    base = _rounded_bg(S, pal)
    d = ImageDraw.Draw(base)
    cx, cy, radius = _gauge(d, S, pal)
    _android(d, S, cx, cy, radius, pal)
    return base.resize((size, size), Image.LANCZOS)


def main():
    os.makedirs(OUT, exist_ok=True)
    for variant, stem in (("dark", "icon"), ("light", "icon-light")):
        img = render(1024, variant)
        png = os.path.join(OUT, f"{stem}.png")
        img.save(png)
        ico = os.path.join(OUT, f"{stem}.ico")
        img.save(ico, sizes=SIZES)
        print(f"Wrote {png}")
        print(f"Wrote {ico}")


if __name__ == "__main__":
    main()
