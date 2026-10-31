"""A MotionCam clip: frames, metadata and audio, with the container details hidden."""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Iterable
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from types import TracebackType
from typing import Any, Literal, Self

import numpy as np
import numpy.typing as npt

from mcrawsome import _raw
from mcrawsome.camera.remosaic import detect_quad_bayer, remosaic
from mcrawsome.io.container import (
    AudioRef,
    read_audio_chunk,
    read_frame_metadata,
    read_frame_record,
    read_index,
)
from mcrawsome.io.metadata import ContainerMetadata, FrameMetadata, FrameTiming, parse_container_metadata
from mcrawsome.io.recorded_motion import RecordedMotion, read_recorded_motion

RAW_DECODE_VERSION = 2  # resume keys must not mix frames made before unflagged Quad Bayer recovery

# How the sensor's colour filter is read. auto: Bayer, or Quad Bayer when flagged or detected (and the
# Quad Bayer preference is on); bayer: always as recorded; quad: always remosaicked from 2x2 cells.
CfaLayout = Literal["auto", "bayer", "quad"]
# The 2x2 Bayer order: the recording's, or one replacing wrong metadata.
CfaPattern = Literal["recorded", "rggb", "bggr", "grbg", "gbrg"]


def remosaic_enabled() -> bool:
    """The saved preference, read when a reader or render job is created."""
    from mcrawsome.server.settings import load_settings

    return load_settings()[0].quad_bayer_remosaic


@dataclass(frozen=True)
class RawFrame:
    index: int
    timestamp_ns: int
    data: npt.NDArray[np.uint16]
    metadata: FrameMetadata


@dataclass(frozen=True)
class AudioTrack:
    samples: npt.NDArray[np.int16]
    sample_rate: int
    first_timestamp_ns: int


class Clip:
    """Read-only access to one .mcraw file. Not shared between processes: open one per worker."""

    def __init__(
        self,
        path: Path,
        *,
        metadata_only: bool = False,
        quad_bayer_remosaic: bool | None = None,
        cfa_layout: CfaLayout = "auto",
        cfa_pattern: CfaPattern = "recorded",
    ) -> None:
        self.path = path
        self.quad_bayer_remosaic = remosaic_enabled() if quad_bayer_remosaic is None else quad_bayer_remosaic
        self.cfa_layout, self.cfa_pattern = cfa_layout, cfa_pattern
        self.timings: dict[str, float] = {}
        self._unflagged_quad: bool | None = None
        self._f = path.open("rb", buffering=0 if metadata_only else -1)
        try:
            stat = os.fstat(self._f.fileno())
            self._stamp = stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns
            self._index = read_index(self._f)
            self.metadata: ContainerMetadata = parse_container_metadata(self._index.metadata)
            if cfa_pattern != "recorded":
                self.metadata = self.metadata.model_copy(update={"sensor_arrangement": cfa_pattern})
        except Exception:
            self._f.close()
            raise
        if not self._index.frames:
            self._f.close()
            raise ValueError(f"{path} contains no frames")

    @property
    def frame_count(self) -> int:
        return len(self._index.frames)

    @property
    def recovered(self) -> bool:
        return self._index.recovered

    @property
    def recovery_note(self) -> str | None:
        if not self.recovered:
            return None
        damaged = self._index.index_damaged
        why = "the frame index was damaged" if damaged else "the recording was not finalised"
        return f"Recovered: {why}; {self.frame_count} frames were found by scanning"

    def _remosaics(self, flagged: bool) -> bool:
        """Whether a frame is read as Quad Bayer: the layout setting first, else the flag or detection."""
        if self.cfa_layout != "auto":
            return self.cfa_layout == "quad"
        return self.quad_bayer_remosaic and flagged

    @property
    def remosaic_note(self) -> str | None:
        """Said when the frames are Quad Bayer, read here as their Bayer mosaic."""
        if self.cfa_layout == "quad":
            return "Read as Quad Bayer (set for this clip): remosaicked to the Bayer pattern"
        if not self.quad_bayer_remosaic or self.cfa_layout == "bayer":
            return None
        if self._unflagged_quad:
            return "Quad Bayer detected in an unbinned sensor crop despite its missing remosaic flag"
        if not self.frame_count or not self.frame_metadata(0).need_remosaic:
            return None
        return "Quad Bayer frames: remosaicked to the Bayer pattern"

    @property
    def timestamps_ns(self) -> npt.NDArray[np.int64]:
        return np.array([fr.timestamp_ns for fr in self._index.frames], dtype=np.int64)

    @property
    def fps(self) -> float:
        ts = self.timestamps_ns
        if len(ts) < 2:
            return 24.0
        return float(1e9 / np.median(np.diff(ts)))

    def frame_metadata(self, i: int) -> FrameMetadata:
        ref = self._index.frames[i]
        return FrameMetadata.model_validate_json(read_frame_metadata(self._f, ref.offset, ref=ref))

    def frame_timing(self, i: int) -> FrameTiming:
        """Validated capture fields, skipping the colour/shading data unused by motion scans."""
        ref = self._index.frames[i]
        return FrameTiming.model_validate_json(read_frame_metadata(self._f, ref.offset, ref=ref))

    def raw_frame_metadata(self, i: int) -> dict[str, Any]:
        """The frame's metadata exactly as MotionCam wrote it."""
        data: dict[str, Any] = json.loads(self.frame_metadata_json(i))
        return data

    def frame_metadata_json(self, i: int) -> bytes:
        """The frame's metadata as MotionCam wrote it, unparsed: about 60 KiB, mostly its lens-shading map."""
        ref = self._index.frames[i]
        return read_frame_metadata(self._f, ref.offset, ref=ref)

    def check_unchanged(self) -> None:
        """Reject analyses made while the source was replaced or modified."""
        for stat in (os.fstat(self._f.fileno()), self.path.stat()):
            if (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns) != self._stamp:
                raise ValueError("Recording changed while reading metadata; refresh and try again")

    @property
    def raw_metadata(self) -> dict[str, Any]:
        """The container's metadata exactly as MotionCam wrote it."""
        return self._index.metadata

    @property
    def audio_chunk_count(self) -> int:
        return len(self._index.audio)

    @property
    def audio_refs(self) -> tuple[AudioRef, ...]:
        """Read-only audio locations, for bounded streaming without joining the whole track."""
        return self._index.audio

    def raw_frame(self, i: int) -> RawFrame:
        return self._frame(i, bin_quad=False)

    @cached_property
    def _first_frame(self) -> FrameMetadata:
        """The immutable layout reference used by Adaptive playback and Quad Bayer detection."""
        return self.frame_metadata(0)

    def draft_frame(self, i: int) -> RawFrame:
        """Adaptive-only Quad Bayer binning; full-resolution readers still remosaic normally."""
        first = self._first_frame
        candidate = first.need_remosaic or (
            self._unflagged_quad is not False
            and first.is_binned is False
            and first.original_width >= 2 * first.width
            and first.original_height >= 2 * first.height
        )
        return self._frame(i, bin_quad=True) if self._remosaics(candidate) else self.raw_frame(i)

    def _frame(self, i: int, *, bin_quad: bool) -> RawFrame:
        started = time.perf_counter()
        ref = self._index.frames[i]
        located = time.perf_counter()
        payload, meta_json = read_frame_record(self._f, ref.offset)
        read = time.perf_counter()
        meta = FrameMetadata.model_validate_json(meta_json)
        parsed = time.perf_counter()
        data = _raw.decode(payload, meta.width, meta.height, meta.compression_type)
        flagged = (
            self.cfa_layout == "auto"
            and self.quad_bayer_remosaic
            and (meta.need_remosaic or self._missing_remosaic(i, data, meta))
        )
        if self._remosaics(flagged):
            if self.cfa_layout == "quad" and (meta.width % 4 or meta.height % 4):
                raise ValueError(
                    f"{meta.width}x{meta.height} frames cannot be read as Quad Bayer: its 2x2 cells need "
                    "sizes in multiples of 4"
                )
            if bin_quad and meta.width % 4 == 0 and meta.height % 4 == 0:
                # Each 2x2 cell records one colour, so its mean is one ordinary Bayer site.
                # Accumulate in uint32 to retain headroom at 16-bit sensor white.
                summed = data[::2, ::2].astype(np.uint32)
                summed += data[1::2, ::2]
                summed += data[::2, 1::2]
                summed += data[1::2, 1::2]
                data = ((summed + 2) // 4).astype(np.uint16)
                meta = meta.model_copy(update={"width": meta.width // 2, "height": meta.height // 2})
            else:
                data = remosaic(data, self.metadata.sensor_arrangement, meta.black_level)
            meta = meta.model_copy(update={"need_remosaic": False})
        self.timings = {
            "lookup_ms": (located - started) * 1000,
            "read_ms": (read - located) * 1000,
            "metadata_ms": (parsed - read) * 1000,
            "decompress_ms": (time.perf_counter() - parsed) * 1000,
        }
        return RawFrame(index=i, timestamp_ns=ref.timestamp_ns, data=data, metadata=meta)

    def _missing_remosaic(self, index: int, data: npt.NDArray[np.uint16], frame: FrameMetadata) -> bool:
        """Freeze an unflagged full-sensor crop's layout from frame zero, never from a changing scene."""
        if self._unflagged_quad is None:
            first = frame if index == 0 else self._first_frame
            self._unflagged_quad = False
            if (
                first.is_binned is False
                and first.original_width >= 2 * first.width
                and first.original_height >= 2 * first.height
            ):
                if index != 0:
                    payload, _ = read_frame_record(self._f, self._index.frames[0].offset)
                    data = _raw.decode(payload, first.width, first.height, first.compression_type)
                self._unflagged_quad = detect_quad_bayer(
                    data, self.metadata.sensor_arrangement, first.black_level
                )
        return self._unflagged_quad

    def independent_reader(self) -> Clip:
        """Reuse this immutable index with a separate file position and metadata instance.

        Opening the path again (rather than duplicating its descriptor) permits concurrent seeks.
        Refuse changed or replaced recordings instead of pairing a new file with the old index.
        The caller owns the returned reader and must close it.
        """
        reader = object.__new__(Clip)
        reader.path = self.path
        reader.quad_bayer_remosaic = self.quad_bayer_remosaic
        reader.cfa_layout, reader.cfa_pattern = self.cfa_layout, self.cfa_pattern
        reader.timings = {}
        reader._unflagged_quad = self._unflagged_quad
        reader._f = self.path.open("rb")
        try:
            for stream in (self._f, reader._f):
                stat = os.fstat(stream.fileno())
                if (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns) != self._stamp:
                    raise OSError("source recording changed before opening its playback reader")
            reader._stamp = self._stamp
            reader._index = self._index
            reader.metadata = self.metadata.model_copy(deep=True)
        except BaseException:
            reader._f.close()
            raise
        return reader

    def audio(self) -> AudioTrack | None:
        """Join audio in container order, including mixed timed and untimed chunks. Untimed chunks
        follow the running end; an untimed first chunk starts at the first video frame. Timestamp jitter
        within the larger of 50 ms and the current chunk's duration leaves samples unchanged. Larger
        gaps get silence and larger overlaps are trimmed. A gap beyond the video duration plus 50 ms is
        logged as a corrupt timestamp and joined end to end. The result holds the audio and capped gaps;
        silence is filled in blocks, but the final allocation covers the whole joined track."""
        if not self._index.audio or not self.metadata.audio_sample_rate:
            return None
        channels = self.metadata.audio_channels or 1
        rate = self.metadata.audio_sample_rate
        refs = self._index.audio
        chunks: list[tuple[int, npt.NDArray[np.int16]]] = []
        first_ts: int | None = None
        length = 0
        video_duration = (self._index.frames[-1].timestamp_ns - self._index.frames[0].timestamp_ns) / 1e9
        video_duration += 1 / self.fps
        gap_limit = round((video_duration + 0.05) * rate)
        for ref in refs:
            ts, samples = read_audio_chunk(self._f, ref.offset)
            if first_ts is None:
                first_ts = ts if ts is not None and ts >= 0 else int(self._index.frames[0].timestamp_ns)
            usable = len(samples) - len(samples) % channels
            block = samples[:usable].reshape(-1, channels)
            start = round((ts - first_ts) * rate / 1e9) if ts is not None and ts >= 0 else length
            offset = start - length
            tolerance = max(round(0.05 * rate), len(block))
            if offset > gap_limit:
                logging.getLogger(__name__).warning(
                    "%s: corrupt audio timestamp at chunk offset %d; joining without the %.3f s gap",
                    self.path.name,
                    ref.offset,
                    offset / rate,
                )
                start = length
            elif offset < -tolerance:
                block = block[-offset:]
                start = length
            elif offset <= tolerance:
                start = length
            chunks.append((start, block))
            length = start + len(block)
        joined = np.empty((length, channels), np.int16)
        written = 0
        for start, block in chunks:
            for offset in range(written, start, 1 << 20):
                joined[offset : min(offset + (1 << 20), start)] = 0
            joined[start : start + len(block)] = block
            written = start + len(block)
        assert first_ts is not None
        return AudioTrack(samples=joined, sample_rate=rate, first_timestamp_ns=first_ts)

    def gyro_log_path(self) -> Path | None:
        candidate = self.path.with_suffix(".gcsv")
        return candidate if candidate.exists() else None

    @property
    def has_recorded_motion(self) -> bool:
        """Whether the recording carries its own gyro (MotionCam 5.1 and later)."""
        return bool(self._index.gyro)

    def recorded_motion(self) -> RecordedMotion | None:
        """The gyro, accelerometer and lens shifts recorded inside the file; None without gyro samples."""
        if not self._index.gyro:
            return None
        return read_recorded_motion(self._f, self._index)

    def close(self) -> None:
        self._f.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


def close_all(clips: Iterable[Clip]) -> None:
    """Close every clip, even when one fails to close; the first failure is raised after the rest."""
    first: Exception | None = None
    for clip in clips:
        try:
            clip.close()
        except Exception as exc:
            first = first or exc
    if first is not None:
        raise first
