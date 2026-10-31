"""GoPro-format telemetry (GPMF), which MotionCam embeds in Direct Log videos.

GPMF is a list of key-length-value entries: a four-character key, a type character, the size of one
item, a repeat count (big-endian), then the data padded to 4 bytes. Type 0 means the data is more entries
(`DEVC` devices holding `STRM` streams). MotionCam writes one `DEVC` per video frame, each with a gyro
and an accelerometer stream, plus a first `DEVC` describing the camera, preceded by a size and "GPMF".
"""

from __future__ import annotations

import json
import struct
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import numpy.typing as npt

from mcrawsome.io.gyro import GyroLog
from mcrawsome.output.ffmpeg import ffprobe_for

_FORMATS = {
    "b": "b",
    "B": "B",
    "s": "h",
    "S": "H",
    "l": "i",
    "L": "I",
    "f": "f",
    "d": "d",
    "j": "q",
    "J": "Q",
}


class GpmfError(Exception):
    """The telemetry cannot be read."""


@dataclass(frozen=True)
class _Entry:
    key: str
    kind: str
    size: int
    repeat: int
    body: bytes
    offset: int


@dataclass(frozen=True)
class SensorStream:
    name: str  # GYRO or ACCL
    stmp_us: int | None
    scale: float
    samples: npt.NDArray[np.float64]  # raw values, shape (n, 3)


@dataclass(frozen=True)
class GpmfStreams:
    blocks: list[dict[str, SensorStream]]  # one per DEVC that carries sensor data, in order
    device: dict[str, str] = field(default_factory=dict)
    orin: str | None = None
    orio: str | None = None


def _entries(buf: bytes, base: int) -> list[_Entry]:
    out, pos = [], 0
    while pos + 8 <= len(buf):
        key = buf[pos : pos + 4].decode("latin1")
        kind = chr(buf[pos + 4])
        size, repeat = buf[pos + 5], struct.unpack(">H", buf[pos + 6 : pos + 8])[0]
        length = size * repeat
        if pos + 8 + length > len(buf):
            raise GpmfError(f"entry {key!r} at offset {base + pos} runs past the end of the data")
        out.append(_Entry(key, kind, size, repeat, buf[pos + 8 : pos + 8 + length], base + pos))
        pos += 8 + length + (-length % 4)
    return out


def _numbers(entry: _Entry) -> tuple[float, ...]:
    fmt = _FORMATS.get(entry.kind)
    if fmt is None:
        raise GpmfError(f"entry {entry.key!r} at offset {entry.offset} has unsupported type {entry.kind!r}")
    count = len(entry.body) // struct.calcsize(fmt)
    return struct.unpack(f">{count}{fmt}", entry.body[: count * struct.calcsize(fmt)])


def _text(entry: _Entry) -> str:
    return entry.body.decode("latin1", "replace").rstrip("\0")


def read_gpmf(data: bytes) -> GpmfStreams:
    start = 8 if data[4:8] == b"GPMF" else 0
    device: dict[str, str] = {}
    blocks: list[dict[str, SensorStream]] = []
    orin = orio = None
    for devc in _entries(data[start:], start):
        if devc.key != "DEVC" or devc.kind != "\0":
            continue
        streams: dict[str, SensorStream] = {}
        for entry in _entries(devc.body, devc.offset + 8):
            if entry.kind == "c" and entry.key != "STRM":
                device.setdefault(entry.key, _text(entry))
            if entry.key != "STRM":
                continue
            stmp: int | None = None
            scale = 1.0
            for item in _entries(entry.body, entry.offset + 8):
                if item.key == "STMP":
                    stmp = int(_numbers(item)[0])
                elif item.key == "SCAL":
                    scale = float(_numbers(item)[0])
                elif item.key == "ORIN":
                    orin = _text(item)
                elif item.key == "ORIO":
                    orio = _text(item)
                elif item.key in ("GYRO", "ACCL"):
                    # an empty payload (marked EMPT) has no samples and an undefined type byte
                    values = (
                        np.asarray(_numbers(item), dtype=np.float64).reshape(-1, 3)
                        if item.repeat
                        else np.zeros((0, 3))
                    )
                    streams[item.key] = SensorStream(item.key, stmp, scale, values)
        if "GYRO" in streams:
            blocks.append(streams)
    return GpmfStreams(blocks=blocks, device=device, orin=orin, orio=orio)


def _reorder(values: npt.NDArray[np.float64], orin: str | None, orio: str | None) -> npt.NDArray[np.float64]:
    """Stored channels are named by ORIN (lower case means negated); return them in ORIO order."""
    if not orin or not orio or len(orin) != 3 or len(orio) != 3:
        return values
    names = orin.upper()
    columns = []
    for axis in orio.upper():
        i = names.index(axis)
        columns.append(values[:, i] * (-1.0 if orin[i].islower() else 1.0))
    return np.stack(columns, -1)


def _times(starts_us: list[int | None], counts: list[int]) -> npt.NDArray[np.float64]:
    starts = np.array([s if s is not None else np.nan for s in starts_us], dtype=np.float64) / 1e6
    if np.isnan(starts).any():
        raise GpmfError("a telemetry block has no timestamp (STMP)")
    durations = np.diff(starts)
    last = float(np.median(durations)) if len(durations) else 1 / 24
    ends = np.append(starts[1:], starts[-1] + last)
    return np.concatenate(
        [s + (e - s) * np.arange(n) / n for s, e, n in zip(starts, ends, counts, strict=True)]
    )


def gyro_log_from_gpmf(streams: GpmfStreams) -> GyroLog:
    if not streams.blocks:
        raise GpmfError("the telemetry has no gyro data")
    gyro_blocks = [b["GYRO"] for b in streams.blocks]
    gyro = np.concatenate([g.samples / g.scale for g in gyro_blocks])
    times = _times([g.stmp_us for g in gyro_blocks], [len(g.samples) for g in gyro_blocks])
    if not len(times):
        raise GpmfError("the telemetry has no gyro samples")
    accel = None
    if all("ACCL" in b for b in streams.blocks):
        accel = _reorder(
            np.concatenate([b["ACCL"].samples / b["ACCL"].scale for b in streams.blocks]),
            streams.orin,
            streams.orio,
        )
    return GyroLog(
        times_s=times,
        gyro=_reorder(gyro, streams.orin, streams.orio),
        accel=accel,
        start_epoch_s=None,
        orientation=streams.orio or "XYZ",
        header=dict(streams.device),
        clock="video",
    )


def extract_gpmf(ffmpeg: Path, video: Path) -> bytes | None:
    """The video's GoPro-format telemetry track, or None when it has none."""
    if video.suffix.lower() in (".mp4", ".mov"):
        from mcrawsome.io.movie_metadata import MovieMetadataError, read_movie_telemetry

        try:
            return read_movie_telemetry(video)
        except (MovieMetadataError, OSError):
            pass  # fragmented/unsupported containers retain the existing demux fallback
    ffprobe = ffprobe_for(ffmpeg)
    probe = subprocess.run(
        [str(ffprobe), "-v", "error", "-show_streams", "-of", "json", str(video)],
        capture_output=True,
        text=True,
    )
    if probe.returncode != 0:
        raise GpmfError(f"{video.name}: ffprobe could not read the file")
    tracks = [s for s in json.loads(probe.stdout).get("streams", []) if s.get("codec_tag_string") == "gpmd"]
    if not tracks:
        return None
    run = subprocess.run(
        [str(ffmpeg), "-v", "error", "-i", str(video), "-map", f"0:{tracks[0]['index']}", "-c", "copy",
         "-f", "data", "-"],
        capture_output=True,
    )  # fmt: skip
    if run.returncode != 0:
        raise GpmfError(f"{video.name}: could not extract the telemetry track")
    return run.stdout
