#!/usr/bin/env python3
"""Generate Akana PWA icons as PNGs using only the standard library.

No Pillow/cairo dependency: we rasterize an "orbital spark" mark — a bright
white-hot core with a tilted elliptical orbit (cyan→violet) and a small
satellite — by supersampling SDF-based shapes per-pixel and writing minimal
RGBA PNGs by hand. Run once to (re)materialize web_ui/static/icons/.

    venv/bin/python tools/gen_pwa_icons.py

When the artwork changes, bump the ?v= on the icon URLs in
web_ui/manifest.webmanifest so an installed PWA picks up the new app icon
(index.html's <link> tags are content-hash-versioned automatically).
"""

from __future__ import annotations

import math
import struct
import zlib
from pathlib import Path

ICONS_DIR = Path(__file__).resolve().parent.parent / "web_ui" / "static" / "icons"

# Palette — matches the cockpit's dark theme + cyan/violet accent.
BG_INNER = "#0c1230"   # deep indigo behind the mark
BG_OUTER = "#05060d"   # == <meta theme-color> edge
VIOLET_BLOOM = "#3a2a7a"
ACCENT = "#38e3ff"     # cyan
VIOLET = "#9b6bff"     # orbit far side / contrast
CORE = "#eafdff"       # white-hot center


def _clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return lo if v < lo else hi if v > hi else v


def _smoothstep(e0: float, e1: float, x: float) -> float:
    t = _clamp((x - e0) / (e1 - e0))
    return t * t * (3 - 2 * t)


def _mix(a: tuple, b: tuple, t: float) -> tuple:
    return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, a[2] + (b[2] - a[2]) * t)


def _add(a: tuple, b: tuple, s: float = 1.0) -> tuple:
    return (a[0] + b[0] * s, a[1] + b[1] * s, a[2] + b[2] * s)


def _hx(s: str) -> tuple:
    s = s.lstrip("#")
    return (int(s[0:2], 16) / 255, int(s[2:4], 16) / 255, int(s[4:6], 16) / 255)


def _sd_round_box(px: float, py: float, b: float, r: float) -> float:
    qx = abs(px) - b + r
    qy = abs(py) - b + r
    return math.hypot(max(qx, 0.0), max(qy, 0.0)) + min(max(qx, qy), 0.0) - r


def _shade(nx: float, ny: float, *, maskable: bool) -> tuple:
    """Return straight (r, g, b, a) in 0..1 for normalized coords in [-1, 1]."""
    if maskable:
        alpha = 1.0
    else:
        alpha = _smoothstep(0.012, -0.012, _sd_round_box(nx, ny, 0.97, 0.42))
        if alpha <= 0:
            return (0.0, 0.0, 0.0, 0.0)

    m = 0.82 if maskable else 1.0  # shrink mark into the maskable safe zone
    x, y = nx / m, ny / m
    r = math.hypot(x, y)

    accent, violet, core = _hx(ACCENT), _hx(VIOLET), _hx(CORE)

    # backdrop: radial indigo + soft violet bloom
    col = _mix(_hx(BG_INNER), _hx(BG_OUTER), _smoothstep(-1.0, 1.0, r))
    col = _add(col, _hx(VIOLET_BLOOM), 0.45 * math.exp(-(r / 0.55) ** 2))

    # tilted elliptical orbit (rotate then squash)
    th = math.radians(-28)
    rx = x * math.cos(th) - y * math.sin(th)
    ry = x * math.sin(th) + y * math.cos(th)
    a_ax, b_ax = 0.78, 0.34
    ed = abs(math.hypot(rx / a_ax, ry / b_ax) - 1.0) * min(a_ax, b_ax)
    orbit = math.exp(-(ed / 0.020) ** 2)
    col = _add(col, _mix(accent, violet, _smoothstep(-0.8, 0.8, rx)), 1.0 * orbit)

    # satellite dot riding the orbit (upper-right)
    sx, sy = a_ax * math.cos(math.radians(35)), b_ax * math.sin(math.radians(35))
    wx = sx * math.cos(-th) - sy * math.sin(-th)
    wy = sx * math.sin(-th) + sy * math.cos(-th)
    sat = math.exp(-(math.hypot(x - wx, y - wy) / 0.055) ** 2)
    col = _add(col, accent, 1.15 * sat)

    # central core: cyan halo + white-hot center with bloom
    col = _add(col, accent, 0.95 * math.exp(-(r / 0.23) ** 2))
    col = _mix(col, core, _clamp(math.exp(-(r / 0.115) ** 2) * 1.25))

    return (col[0], col[1], col[2], alpha)


def _render(size: int, *, maskable: bool, ss: int) -> bytearray:
    """Supersample (ss×ss) with premultiplied averaging for clean edges."""
    px = bytearray(size * size * 4)
    n = ss * ss
    i = 0
    for oy in range(size):
        for ox in range(size):
            ar = ag = ab = aa = 0.0
            for sy in range(ss):
                for sx in range(ss):
                    nxx = ((ox + (sx + 0.5) / ss) / size) * 2 - 1
                    nyy = ((oy + (sy + 0.5) / ss) / size) * 2 - 1
                    cr, cg, cb, al = _shade(nxx, nyy, maskable=maskable)
                    ar += cr * al
                    ag += cg * al
                    ab += cb * al
                    aa += al
            if aa > 0:
                px[i] = int(_clamp(ar / aa) * 255 + 0.5)
                px[i + 1] = int(_clamp(ag / aa) * 255 + 0.5)
                px[i + 2] = int(_clamp(ab / aa) * 255 + 0.5)
            px[i + 3] = int(_clamp(aa / n) * 255 + 0.5)
            i += 4
    return px


def _chunk(tag: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)


def _write_png(path: Path, size: int, rgba: bytearray) -> None:
    rows = bytearray()
    stride = size * 4
    for y in range(size):
        rows.append(0)  # filter type 0 (None)
        rows.extend(rgba[y * stride:(y + 1) * stride])
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)  # 8-bit RGBA
    png = b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", zlib.compress(bytes(rows), 9)) + _chunk(b"IEND", b"")
    path.write_bytes(png)


def main() -> None:
    ICONS_DIR.mkdir(parents=True, exist_ok=True)
    targets = [
        ("icon-192.png", 192, False, 4),
        ("icon-512.png", 512, False, 3),
        ("icon-maskable-512.png", 512, True, 3),
    ]
    for name, size, maskable, ss in targets:
        out = ICONS_DIR / name
        _write_png(out, size, _render(size, maskable=maskable, ss=ss))
        print(f"wrote {out} ({size}x{size}{', maskable' if maskable else ''})")


if __name__ == "__main__":
    main()
