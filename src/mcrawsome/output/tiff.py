"""A small little-endian TIFF writer, shared by the DNG, 16-bit TIFF and JPEG EXIF writers.

Entries are (tag, type, count, data) with `data` already packed; `write_tiff` lays one IFD out with its
values after it and the image blocks (strips or tiles) after those, aligned to 16 bytes.
"""

from __future__ import annotations

import struct
from fractions import Fraction
from pathlib import Path
from typing import Any

BYTE, ASCII, SHORT, LONG, RATIONAL, UNDEFINED, SRATIONAL, FLOAT, DOUBLE = 1, 2, 3, 4, 5, 7, 10, 11, 12
SIZES = {BYTE: 1, ASCII: 1, SHORT: 2, LONG: 4, RATIONAL: 8, UNDEFINED: 1, SRATIONAL: 8, FLOAT: 4,
         DOUBLE: 8}  # fmt: skip
STRIP_OFFSETS, TILE_OFFSETS = 273, 324

Entry = tuple[int, int, int, bytes]


def _rational(values: tuple[float, ...], signed: bool) -> tuple[int, bytes]:
    pairs = [Fraction(v).limit_denominator(1_000_000) for v in values]
    fmt = "<" + ("ii" if signed else "II") * len(pairs)
    return len(pairs), struct.pack(fmt, *[n for f in pairs for n in (f.numerator, f.denominator)])


def entry(tag: int, kind: int, values: Any) -> Entry:
    if kind == ASCII:
        data = values.encode("ascii", "replace") + b"\0"
        return tag, kind, len(data), data
    if kind in (BYTE, UNDEFINED):
        data = bytes(values)
        return tag, kind, len(data), data
    if kind in (RATIONAL, SRATIONAL):
        count, data = _rational(tuple(values), kind == SRATIONAL)
        return tag, kind, count, data
    if kind == DOUBLE:
        return tag, kind, len(values), struct.pack(f"<{len(values)}d", *values)
    fmt = {SHORT: "H", LONG: "I"}[kind]
    return tag, kind, len(values), struct.pack(f"<{len(values)}{fmt}", *values)


def tiff_header(
    entries: list[Entry], block_sizes: list[int], *, alignment: int = 16, padding: int = 0
) -> bytes:
    """Lay out an IFD without allocating its pixels, for physical and virtual TIFF files."""
    entries = sorted(entries, key=lambda e: e[0])
    ifd_size = 2 + 12 * len(entries) + 4
    extra_at = 8 + ifd_size
    extra = bytearray()
    fields: list[bytes] = []
    layout: list[tuple[int, int, int, bytes, int | None]] = []
    for tag, kind, count, data in entries:
        if len(data) <= 4:
            layout.append((tag, kind, count, data, None))
        else:
            if len(extra) % 2:
                extra += b"\0"
            layout.append((tag, kind, count, data, extra_at + len(extra)))
            extra += data
    metadata_end = extra_at + len(extra)
    image_at = metadata_end + padding + (-(metadata_end + padding) % alignment)
    starts, position = [], image_at
    for size in block_sizes:
        starts.append(position)
        position += size
    offsets = struct.pack(f"<{len(starts)}I", *starts)
    for tag, kind, count, data, offset in layout:
        if tag in (STRIP_OFFSETS, TILE_OFFSETS):
            if offset is None:
                data = offsets
            else:
                start = offset - extra_at
                extra[start : start + len(offsets)] = offsets
        value = data.ljust(4, b"\0") if offset is None else struct.pack("<I", offset)
        fields.append(struct.pack("<HHI", tag, kind, count) + value)
    header = b"II*\0" + struct.pack("<I", 8) + struct.pack("<H", len(entries)) + b"".join(fields)
    header += struct.pack("<I", 0) + bytes(extra)
    return header.ljust(image_at, b"\0")


def write_tiff(path: Path, entries: list[Entry], blocks: list[bytes]) -> None:
    """One IFD and its image blocks, with strip or tile offsets filled in."""
    header = tiff_header(entries, [len(block) for block in blocks])
    with path.open("wb") as f:
        f.write(header)
        for block in blocks:
            f.write(block)
