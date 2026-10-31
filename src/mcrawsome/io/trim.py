"""Cut a range of frames, with the audio and recorded motion that overlap it, into a new .mcraw file."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import numpy as np

from mcrawsome.io.container import (
    ContainerWriter,
    ItemType,
    read_audio_chunk,
    read_frame_record,
    read_index,
    read_motion,
)

MOTION_MARGIN_NS = 500_000_000  # motion kept either side of the frames, for interpolation and smoothing


def trim(
    src: Path,
    dst: Path,
    start: int = 0,
    count: int | None = None,
    *,
    on_frame: Callable[[int, int], None] | None = None,
) -> int:
    """Copy frames [start, start + count) without re-compressing; return the audio chunk count.
    With no count, copy all remaining frames and all audio. The existing reader also handles recordings
    that were not finalised. `on_frame` is called after each copied frame."""
    if dst.exists():
        raise FileExistsError(f"{dst} already exists")
    with src.open("rb") as f:
        index = read_index(f)
        total = len(index.frames)
        wanted = total - start if count is None else count
        if start < 0 or wanted < 1 or start + wanted > total:
            raise ValueError(f"no frames in range start={start} count={count}")
        frames = index.frames[start : start + wanted]
        first, last = frames[0].timestamp_ns, frames[-1].timestamp_ns
        period = (
            round(float(np.median(np.diff([fr.timestamp_ns for fr in index.frames]))))
            if total > 1
            else round(1e9 / 24)
        )
        end = index.frames[start + wanted].timestamp_ns if start + wanted < total else last + period
        extra = index.metadata.get("extraData") or {}
        rate, channels = extra.get("audioSampleRate"), extra.get("audioChannels") or 1
        audio_count = 0
        untimed = index.frames[0].timestamp_ns
        with ContainerWriter(dst, index.metadata) as w:
            for i, fr in enumerate(frames):
                payload, meta = read_frame_record(f, fr.offset)
                w.add_frame(fr.timestamp_ns, payload, meta)
                if on_frame is not None:
                    on_frame(i + 1, wanted)
            for ref in index.audio:
                ts, samples = read_audio_chunk(f, ref.offset)
                when = ts if ts is not None and ts >= 0 else ref.timestamp_ns
                if when < 0:
                    when = untimed
                duration = round(len(samples) / channels / rate * 1e9) if rate else 0
                untimed = when + duration
                if count is None or (when < end and (when + duration > first or when == first)):
                    w.add_audio(samples.tobytes(), ts)
                    audio_count += 1
            for item_type, chunks in (
                (ItemType.GYRO_DATA, index.gyro),
                (ItemType.ACCELEROMETER_DATA, index.accelerometer),
                (ItemType.OIS_DATA, index.ois),
            ):
                for chunk in chunks:
                    motion = read_motion(f, (chunk,), item_type)
                    if count is not None:
                        times = motion["t"]
                        motion = motion[
                            (times >= first - MOTION_MARGIN_NS) & (times < end + MOTION_MARGIN_NS)
                        ]
                    if len(motion):
                        w.add_motion(item_type, motion)
        return audio_count
