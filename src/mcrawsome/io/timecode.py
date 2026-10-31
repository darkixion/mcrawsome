"""Time-of-day timecode from the phone's clock.

A RAW clip's frames carry the phone's wall-clock receive times. The smallest receive latency over the
first frames links the sensor clock to the wall clock, and the camera profile's gyro sync offset removes
the remaining fixed delay, so the first frame's capture time is known to the phone's clock (network time:
typically within a frame or two between devices). Timecode counts frames from local midnight in a chosen
time zone at the clip's whole-number rate, non-drop-frame. Rates above 30 count pairs (or groups) of frames
per timecode frame, as SMPTE 12M does for 50 and 60 fps.
"""

from __future__ import annotations

import contextlib
import math
import os
import struct
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import numpy as np
import numpy.typing as npt

if TYPE_CHECKING:
    from mcrawsome.io.clip import Clip

LINK_FRAMES = 48  # receive times read to find the smallest latency
TIMECODE_VERSION = 1  # part of a mount's identity: the served bytes depend on it


def local_zone() -> str:
    """This computer's IANA time zone (TZ, /etc/timezone or /etc/localtime), else UTC."""
    candidates = [os.environ.get("TZ", "").lstrip(":")]
    with contextlib.suppress(OSError):
        candidates.append(Path("/etc/timezone").read_text().strip())
    try:
        target = os.readlink("/etc/localtime")
        if "zoneinfo/" in target:
            candidates.append(target.split("zoneinfo/", 1)[1])
    except OSError:
        pass
    for name in candidates:
        if name and valid_zone(name):
            return name
    return "UTC"


def valid_zone(name: str) -> bool:
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return False
    return True


def capture_start(
    timestamps_ns: npt.NDArray[np.int64], received_ms: npt.NDArray[np.float64], sync_ms: float | None
) -> float:
    """The first frame's capture time, seconds since 1970 (UTC) on the phone's clock: the receive-time
    link with the smallest latency (over the frames given), corrected by the camera's sync offset."""
    count = min(len(timestamps_ns), len(received_ms))
    if count == 0:
        raise ValueError("no frame receive times to link the clip to the wall clock")
    stamps = np.asarray(timestamps_ns[:count], dtype=np.int64)
    received = np.asarray(received_ms[:count], dtype=np.int64)
    if np.any(received <= 0):
        raise ValueError("the recording has no frame receive times")
    offset = int((received * 1_000_000 - stamps).min())
    return (int(stamps[0]) + offset) / 1e9 + (sync_ms or 0) / 1000


@dataclass(frozen=True)
class Timecode:
    """Where a served sequence's timecode starts, and how it counts."""

    zone: str
    start_frames: int  # frames since local midnight at the first served frame, at `per_second`
    per_second: int  # timecode frames per second (30 at most)
    group: int  # output frames per timecode frame (1, or 2 for 50 and 60 fps)
    start_local: datetime

    @classmethod
    def at(cls, epoch_s: float, rate: float, zone: str) -> Timecode:
        """The timecode of a frame captured at `epoch_s`, for a sequence at `rate`, in `zone`."""
        if not valid_zone(zone):
            raise ValueError(f"unknown time zone {zone!r}")
        whole = max(1, round(rate))
        group = max(1, math.ceil(whole / 30))
        per_second = max(1, round(whole / group))
        local = datetime.fromtimestamp(epoch_s, UTC).astimezone(ZoneInfo(zone))
        midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
        seconds = (local - midnight).total_seconds()
        return cls(zone, math.floor(seconds * whole), per_second, group, local)

    def frame(self, number: int) -> tuple[int, int, int, int, bool]:
        """(hours, minutes, seconds, frames, second of a pair) of served frame `number`."""
        total = self.start_frames + number
        count, second = divmod(total, self.group)
        count %= 24 * 3600 * self.per_second
        frames = count % self.per_second
        seconds = count // self.per_second
        return seconds // 3600, seconds // 60 % 60, seconds % 60, frames, self.group == 2 and second == 1

    def text(self, number: int = 0) -> str:
        hours, minutes, seconds, frames, _ = self.frame(number)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}:{frames:02d}"

    def address(self, number: int) -> bytes:
        """CinemaDNG TimeCodes (tag 51043): the SMPTE 12M time address and empty binary groups."""
        hours, minutes, seconds, frames, second = self.frame(number)

        def bcd(value: int) -> int:
            return (value // 10) << 4 | value % 10

        return bytes([bcd(frames), bcd(seconds) | (0x80 if second else 0), bcd(minutes), bcd(hours)]) + bytes(
            4
        )

    def time_reference(self, sample_rate: int) -> int:
        """BWF TimeReference: samples since local midnight at the first served frame, so the sound's
        timecode equals the first frame's."""
        return self.start_frames * sample_rate // (self.per_second * self.group)


def bext(time_reference: int, start_local: datetime, description: str = "") -> bytes:
    """A Broadcast Wave `bext` chunk (EBU Tech 3285, version 1) carrying the time reference."""
    body = struct.pack(
        "<256s32s32s10s8sQH64s190s",
        description.encode("ascii", "replace")[:256],
        b"MCRAWsome",
        b"",
        start_local.strftime("%Y-%m-%d").encode(),
        start_local.strftime("%H:%M:%S").encode(),
        time_reference,
        1,
        b"",
        b"",
    )
    return b"bext" + struct.pack("<I", len(body)) + body


def clip_start(clip: Clip) -> float:
    """A RAW clip's first frame capture time (seconds since 1970, UTC), corrected by its camera profile's
    gyro sync offset when one matches."""
    from mcrawsome.camera.profile import ProfileError, find_profile

    count = min(clip.frame_count, LINK_FRAMES)
    received = np.array([clip.frame_timing(index).received_ms for index in range(count)], dtype=np.float64)
    first, meta = clip.frame_metadata(0), clip.metadata
    try:
        profile = find_profile(meta.device_model, meta.camera_id, first.width, first.height, clip.fps)
    except ProfileError:
        profile = None
    return capture_start(clip.timestamps_ns, received, profile.gcsv_sync_ms if profile else None)


def clip_timecode(clip: Clip, zone: str, rate: float, offset_s: float = 0.0) -> Timecode:
    """Time-of-day timecode for a RAW clip's frames at `rate`, from `offset_s` after its first frame."""
    return Timecode.at(clip_start(clip) + offset_s, rate, zone)


def video_start(path: Path) -> float | None:
    """A Direct Log video's start (seconds since 1970, UTC): the container's creation time, else the
    YYMMDD_HHMMSS at the start of MotionCam's file name, read in this computer's time zone. To the second."""
    import re

    from mcrawsome.io.directlog import probe_direct_log
    from mcrawsome.output.ffmpeg import find_ffmpeg

    created = probe_direct_log(find_ffmpeg()[1], path).created
    if created is not None:
        return created.timestamp()
    match = re.match(r"(\d{6})_(\d{6})", path.name)
    if match is None:
        return None
    try:
        local = datetime.strptime("".join(match.groups()), "%y%m%d%H%M%S").replace(
            tzinfo=ZoneInfo(local_zone())
        )
    except ValueError:
        return None
    return local.timestamp()


def export_timecode(clip_path: Path, choice: str, zone: str | None, rate: float) -> Timecode | None:
    """An export's time-of-day timecode, counted from the clip's first frame (frame numbers stay those of
    the whole clip); None for timecode from zero, or when the capture time is unknown."""
    if choice != "time-of-day":
        return None
    from mcrawsome.io.directlog import is_video_input

    zone = zone or local_zone()
    if is_video_input(clip_path):
        start = video_start(clip_path)
        return None if start is None else Timecode.at(start, rate, zone)
    from mcrawsome.io.clip import Clip

    with Clip(clip_path, metadata_only=True) as clip:
        return clip_timecode(clip, zone, rate)


def from_frame(code: Timecode, first: int) -> Timecode:
    """The same timecode, starting `first` frames later (for sound that starts with a trimmed export)."""
    from dataclasses import replace

    return replace(code, start_frames=code.start_frames + first)


def timecode_note(code: Timecode | None, first: int = 0) -> str:
    """What an export says about its time-of-day timecode."""
    if code is None:
        return "time-of-day timecode: the capture time is unknown, so the timecode starts from zero"
    return f"time-of-day timecode from {code.text(first)} ({code.zone})"
