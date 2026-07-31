#!/usr/bin/env python3
"""Draw the menu bar icon.

A template image, which on macOS means the colour is thrown away and only the
alpha channel is kept — the system then paints the shape to suit a light or dark
menu bar. Handing it the app icon instead produced a solid black square, because
an opaque square is exactly what its alpha channel says it is.

The shape is three bars of different heights: the level meter this app puts on
its recording screen, which is the one picture of itself it already uses.

    python3 menubar.py    rewrites menubar.png beside this file
"""

import struct
import zlib
from pathlib import Path

SIZE = 44                      # 22pt at 2x, the usual menu bar height
BARS = ((7, 14), (19, 26), (31, 18))   # (left edge, height), 6px wide each
WIDTH = 6
RADIUS = 2


def covered(x: int, y: int) -> bool:
    """Whether this pixel is inside one of the bars, corners rounded off."""
    for left, height in BARS:
        top = (SIZE - height) // 2
        if not (left <= x < left + WIDTH and top <= y < top + height):
            continue
        # Distance from the nearest corner, so the ends read as rounded rather
        # than cut. Cheap and good enough at this size.
        dx = min(x - left, left + WIDTH - 1 - x)
        dy = min(y - top, top + height - 1 - y)
        if dx < RADIUS and dy < RADIUS:
            near = (RADIUS - 1 - dx) ** 2 + (RADIUS - 1 - dy) ** 2
            return near <= RADIUS * RADIUS - 1
        return True
    return False


def png(path: Path) -> None:
    rows = bytearray()
    for y in range(SIZE):
        rows.append(0)                       # no per-row filter
        for x in range(SIZE):
            # Black throughout. Only the alpha carries the shape, which is the
            # whole contract of a template image.
            rows += bytes((0, 0, 0, 255 if covered(x, y) else 0))

    def chunk(kind: bytes, body: bytes) -> bytes:
        return (struct.pack(">I", len(body)) + kind + body
                + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF))

    header = struct.pack(">IIBBBBB", SIZE, SIZE, 8, 6, 0, 0, 0)   # 8-bit RGBA
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
                     + chunk(b"IDAT", zlib.compress(bytes(rows), 9))
                     + chunk(b"IEND", b""))


if __name__ == "__main__":
    out = Path(__file__).with_name("menubar.png")
    png(out)
    print(f"wrote {out} ({out.stat().st_size} bytes)")
