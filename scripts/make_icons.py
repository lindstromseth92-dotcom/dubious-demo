"""Generate the extension icons (a line of text with a red squiggle under it).

Pure standard library: writes PNGs by hand so no imaging dependency is needed.
Run from the project root:  uv run python scripts/make_icons.py
"""

import math
import struct
import zlib
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "extension" / "icons"
BG = (29, 32, 38)
BAR = (238, 240, 243)
SQUIGGLE = (240, 68, 68)
SS = 4  # supersampling factor per axis


def rounded_rect(x, y, x0, y0, x1, y1, r):
    cx = min(max(x, x0 + r), x1 - r)
    cy = min(max(y, y0 + r), y1 - r)
    return math.hypot(x - cx, y - cy) <= r


def on_squiggle(x, y):
    if not 0.17 <= x <= 0.83:
        return False
    amp, periods, mid, half = 0.07, 2.0, 0.68, 0.036
    phase = (x - 0.17) / 0.66 * periods * 2 * math.pi
    fy = mid + amp * math.sin(phase)
    slope = amp * math.cos(phase) * periods * 2 * math.pi / 0.66
    return abs(y - fy) / math.sqrt(1 + slope * slope) <= half


def sample(x, y):
    """Colour and alpha at a point in the unit square."""
    if not rounded_rect(x, y, 0.0, 0.0, 1.0, 1.0, 0.22):
        return None
    if on_squiggle(x, y):
        return SQUIGGLE
    if rounded_rect(x, y, 0.17, 0.27, 0.83, 0.41, 0.07):
        return BAR
    return BG


def render(size):
    rows = []
    n = SS * SS
    for py in range(size):
        row = bytearray([0])  # filter type 0
        for px in range(size):
            r = g = b = a = 0
            for sy in range(SS):
                for sx in range(SS):
                    c = sample((px + (sx + 0.5) / SS) / size, (py + (sy + 0.5) / SS) / size)
                    if c:
                        r += c[0]
                        g += c[1]
                        b += c[2]
                        a += 1
            if a:
                row += bytes((r // a, g // a, b // a, round(255 * a / n)))
            else:
                row += bytes((0, 0, 0, 0))
        rows.append(bytes(row))
    return b"".join(rows)


def chunk(kind, data):
    body = kind + data
    return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)


def png(size):
    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)  # 8-bit RGBA
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(render(size), 9)) + chunk(b"IEND", b"")


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    for size in (16, 32, 48, 128):
        (OUT / f"icon{size}.png").write_bytes(png(size))
        print(f"wrote icon{size}.png")
