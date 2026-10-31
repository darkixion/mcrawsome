"""Read ISO BMFF sample tables and telemetry by seeking past media payloads.

Only the bounded moov box and selected gpmd samples are read; no video/audio
packets are demuxed or decoded. Fragmented movies are deliberately rejected.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path

import numpy as np
import numpy.typing as npt

MAX_MOOV = 64 << 20
MAX_SAMPLES = 2_000_000
MAX_TELEMETRY = 32 << 20


class MovieMetadataError(ValueError):
    pass


def boxes(data: bytes) -> list[tuple[bytes, bytes]]:
    result = []
    pos = 0
    while pos < len(data):
        if pos + 8 > len(data):
            raise MovieMetadataError("Truncated movie metadata box")
        size, tag = struct.unpack_from(">I4s", data, pos)
        header = 8
        if size == 1:
            if pos + 16 > len(data):
                raise MovieMetadataError("Truncated extended movie box")
            size = struct.unpack_from(">Q", data, pos + 8)[0]
            header = 16
        if size == 0:
            size = len(data) - pos
        if size < header or pos + size > len(data):
            raise MovieMetadataError("Invalid movie metadata box size")
        result.append((tag, data[pos + header : pos + size]))
        pos += size
    return result


def _child(data: bytes, tag: bytes) -> bytes:
    return next((body for name, body in boxes(data) if name == tag), b"")


def _table(data: bytes, columns: int, code: str = "I") -> list[tuple[int, ...]]:
    if len(data) < 8:
        raise MovieMetadataError("Missing movie sample table")
    count = struct.unpack_from(">I", data, 4)[0]
    row = struct.Struct(">" + code * columns)
    if count > MAX_SAMPLES or len(data) != 8 + count * row.size:
        raise MovieMetadataError("Invalid or oversized movie sample table")
    return list(row.iter_unpack(data[8:]))


@dataclass(frozen=True)
class MovieMetadata:
    times_ns: npt.NDArray[np.int64]
    telemetry_ranges: tuple[tuple[int, int], ...]


def read_movie_metadata(path: Path) -> MovieMetadata:
    moov = b""
    length = path.stat().st_size
    with path.open("rb", buffering=0) as stream:
        pos = 0
        while pos + 8 <= length:
            stream.seek(pos)
            raw = stream.read(8)
            if len(raw) != 8:
                raise MovieMetadataError("Truncated movie")
            size, tag = struct.unpack(">I4s", raw)
            header = 8
            if size == 1:
                raw = stream.read(8)
                if len(raw) != 8:
                    raise MovieMetadataError("Truncated extended movie box")
                size = struct.unpack(">Q", raw)[0]
                header = 16
            if size == 0:
                size = length - pos
            if size < header or pos + size > length:
                raise MovieMetadataError("Invalid movie box size")
            if tag == b"moof":
                raise MovieMetadataError("Fragmented movie: no complete static sample table")
            if tag == b"moov":
                if size - header > MAX_MOOV:
                    raise MovieMetadataError("Movie metadata exceeds 64 MiB")
                moov = stream.read(size - header)
                if len(moov) != size - header:
                    raise MovieMetadataError("Truncated movie metadata")
            pos += size
    if not moov:
        raise MovieMetadataError("No ISO BMFF movie metadata")
    times: list[int] | None = None
    telemetry = []
    for tag, track in boxes(moov):
        if tag != b"trak":
            continue
        media = _child(track, b"mdia")
        handler = _child(media, b"hdlr")
        table = _child(_child(media, b"minf"), b"stbl")
        if not table:
            continue
        entries = dict(boxes(table))
        if handler[8:12] == b"vide" and times is None:
            mdhd = _child(media, b"mdhd")
            offset = 20 if mdhd[:1] == b"\x01" else 12
            if len(mdhd) < offset + 4:
                raise MovieMetadataError("Missing movie media clock")
            scale = struct.unpack_from(">I", mdhd, offset)[0]
            if not scale:
                raise MovieMetadataError("Invalid movie media clock")
            runs = _table(entries.get(b"stts", b""), 2)
            count = sum(run[0] for run in runs)
            if count > MAX_SAMPLES:
                raise MovieMetadataError("Movie has more than two million samples")
            ticks: list[int] = []
            cursor = 0
            for repeat, delta in runs:
                ticks.extend(range(cursor, cursor + repeat * delta, delta) if delta else [cursor] * repeat)
                cursor += repeat * delta
            ctts = entries.get(b"ctts")
            if ctts:
                shifts: list[int] = []
                for repeat, shift in _table(ctts, 2):
                    if ctts[0] == 1 and shift >= 1 << 31:
                        shift -= 1 << 32
                    if len(shifts) + repeat > count:
                        raise MovieMetadataError("Invalid composition-time table")
                    shifts.extend([shift] * repeat)
                if len(shifts) != count:
                    raise MovieMetadataError("Incomplete composition-time table")
                ticks = [tick + shift for tick, shift in zip(ticks, shifts, strict=True)]
            ticks.sort()
            origin = ticks[0] if ticks else 0
            times = [(tick - origin) * 1_000_000_000 // scale for tick in ticks]
        description = entries.get(b"stsd", b"")
        if len(description) < 8 or not any(tag == b"gpmd" for tag, _ in boxes(description[8:])):
            continue
        stsz = entries.get(b"stsz", b"")
        if len(stsz) < 12:
            raise MovieMetadataError("Missing telemetry sample sizes")
        fixed, count = struct.unpack_from(">II", stsz, 4)
        if count > MAX_SAMPLES or len(stsz) != 12 + (0 if fixed else 4 * count):
            raise MovieMetadataError("Invalid telemetry sample sizes")
        sizes = [fixed] * count if fixed else list(struct.unpack(f">{count}I", stsz[12:]))
        if sum(sizes) > MAX_TELEMETRY:
            raise MovieMetadataError("Telemetry exceeds 32 MiB")
        chunks = _table(
            entries.get(b"stco", entries.get(b"co64", b"")), 1, "I" if b"stco" in entries else "Q"
        )
        layout = _table(entries.get(b"stsc", b""), 3)
        if not layout or layout[0][0] != 1 or any(b[0] <= a[0] for a, b in pairwise(layout)):
            raise MovieMetadataError("Invalid telemetry chunk layout")
        sample = run = 0
        for number, (chunk,) in enumerate(chunks, 1):
            while run + 1 < len(layout) and number >= layout[run + 1][0]:
                run += 1
            per_chunk = layout[run][1]
            if not per_chunk or sample + per_chunk > len(sizes):
                raise MovieMetadataError("Invalid telemetry samples per chunk")
            for size in sizes[sample : sample + per_chunk]:
                if chunk + size > length:
                    raise MovieMetadataError("Telemetry sample outside movie")
                telemetry.append((chunk, size))
                chunk += size
            sample += per_chunk
        if sample != count:
            raise MovieMetadataError("Incomplete telemetry chunk layout")
    if times is None:
        raise MovieMetadataError("No indexed video timestamps")
    return MovieMetadata(np.asarray(times, dtype=np.int64), tuple(telemetry))


def read_movie_telemetry(path: Path) -> bytes | None:
    movie = read_movie_metadata(path)
    if not movie.telemetry_ranges:
        return None
    parts = []
    with path.open("rb", buffering=0) as stream:
        for offset, size in movie.telemetry_ranges:
            stream.seek(offset)
            data = stream.read(size)
            if len(data) != size:
                raise MovieMetadataError("Truncated movie telemetry")
            parts.append(data)
    return b"".join(parts)
