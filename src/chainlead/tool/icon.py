"""ChainLead 512x512 PNG icon, generated deterministically in pure Python (the x402 Bazaar does not
accept SVG). Served at /static/icon.png and used as the manifest `image` and x402 `resource.iconUrl`."""
from __future__ import annotations

import struct
import zlib
from functools import lru_cache

SIZE = 512
BG = (10, 16, 34)
LINK = (0, 82, 255)          # Base blue
NODE = (120, 220, 190)
ARROW = (240, 244, 252)


def _pixel(x: int, y: int) -> tuple[int, int, int]:
    # Three chained nodes on a diagonal (a "chain" of evidence) and an up-right arrow (a qualified lead).
    nodes = [(150, 362), (256, 256), (362, 150)]
    for cx, cy in nodes:
        d2 = (x - cx) ** 2 + (y - cy) ** 2
        if d2 <= 46 ** 2:
            return NODE
        if d2 <= 66 ** 2:
            return LINK
    if abs((x - 150) - (362 - y)) <= 16 and 150 <= x <= 362:
        return LINK
    # arrow head toward the top-right corner
    if 380 <= x <= 470 and 42 <= y <= 132 and (x - 380) >= (y - 42) - 6 and (470 - x) + (y - 42) <= 96:
        return ARROW
    return BG


def _chunk(tag: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)


@lru_cache(maxsize=1)
def png_bytes() -> bytes:
    rows = bytearray()
    for y in range(SIZE):
        rows.append(0)
        for x in range(SIZE):
            rows.extend(_pixel(x, y))
    ihdr = struct.pack(">IIBBBBB", SIZE, SIZE, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", zlib.compress(bytes(rows), 9))
            + _chunk(b"IEND", b""))
