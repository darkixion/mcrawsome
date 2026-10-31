"""A RAW clip's gyro: the motion MotionCam 5.1 and later records inside the .mcraw, else the .gcsv beside it.

Motion recorded inside the clip shares the frames' sensor clock, so it needs no syncing, and comes with the
lens shifts of optical image stabilisation. Its log starts its clock at the first frame's timestamp.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

import numpy as np
import numpy.typing as npt

from mcrawsome.camera.profile import Axes, CameraProfile
from mcrawsome.camera.recorded import RecordedCamera, recorded_camera
from mcrawsome.io.clip import Clip
from mcrawsome.io.container import ContainerError
from mcrawsome.io.gcsv import GcsvError, read_gcsv
from mcrawsome.io.gyro import GyroLog
from mcrawsome.io.recorded_motion import RecordedMotion, Samples
from mcrawsome.motion.sync import raw_frame_times

Array = npt.NDArray[np.float64]


class GyroError(Exception):
    """The clip has a gyro log, but it cannot be read or linked to the frames."""


@dataclass(frozen=True)
class RawGyro:
    log: GyroLog
    frame_times_s: Array  # when each recorded frame's first row starts, on the log's clock
    source: Literal["mcraw", "gcsv"]
    camera: RecordedCamera | None = None  # the recording's own calibration, with recorded motion
    ois: Samples | None = None  # lens shifts, on the log's clock (seconds), with recorded motion

    @property
    def recorded(self) -> bool:
        return self.source == "mcraw"

    def sync_s(self, profile: CameraProfile) -> float:
        """The profile's sync for a .gcsv; none for motion recorded on the frames' own clock."""
        return 0.0 if self.recorded else (profile.gcsv_sync_ms or 0) / 1000

    def picture_log(self, profile: CameraProfile) -> tuple[GyroLog, bool]:
        """The log as the picture moved, and whether OIS lens shifts joined it (see with_lens_shift)."""
        axes, focal = profile.mcraw_axes, profile.focal_px
        if self.ois is None or self.camera is None or axes is None or not focal:
            return self.log, False
        return with_lens_shift(self.log, self.ois, axes, focal, self.camera.shift_scale), True


def has_recorded_motion(path: Path) -> bool:
    """Whether a RAW recording carries its own gyro; False for anything unreadable."""
    try:
        with Clip(path, metadata_only=True) as clip:
            return clip.has_recorded_motion
    except (OSError, ContainerError, ValueError):
        return False


def has_gyro(path: Path) -> bool:
    """Whether a RAW clip has gyro: a .gcsv beside it, or motion recorded inside it."""
    return path.with_suffix(".gcsv").is_file() or has_recorded_motion(path)


def raw_gyro(
    clip: Clip, *, max_log_bytes: int | None = None, link_frames: int | None = None
) -> RawGyro | None:
    """The clip's gyro, or None when it has none. `max_log_bytes` reads only that much of a .gcsv;
    `link_frames` links a .gcsv to the frames through only the first so many frames' receive times."""
    motion = clip.recorded_motion()
    if motion is not None:
        return _recorded(clip, motion)
    path = clip.gyro_log_path()
    if path is None:
        return None
    try:
        log = read_gcsv(path, max_bytes=max_log_bytes)
    except GcsvError as exc:
        raise GyroError(f"the gyro log cannot be read ({exc})") from None
    stamps = clip.timestamps_ns
    count = len(stamps) if link_frames is None else max(1, min(len(stamps), link_frames))
    received = np.array([clip.frame_timing(i).received_ms for i in range(count)], dtype=np.float64)
    try:
        linked = raw_frame_times(stamps[:count], received, log)
    except ValueError as exc:
        raise GyroError(str(exc)) from None
    times = np.asarray((stamps - stamps[0]) / 1e9 + linked[0], dtype=np.float64)
    return RawGyro(log, times, "gcsv")


def _recorded(clip: Clip, motion: RecordedMotion) -> RawGyro:
    stamps = clip.timestamps_ns
    start = int(stamps[0])
    camera = recorded_camera(clip.metadata, clip.frame_timing(0))
    gyro = motion.gyro
    times = (gyro.times_ns - start) / 1e9
    accel = None
    if motion.accelerometer is not None and len(motion.accelerometer.times_ns) >= 2:
        moments = (motion.accelerometer.times_ns - start) / 1e9
        accel = np.stack([np.interp(times, moments, motion.accelerometer.values[:, k]) for k in range(3)], -1)
    log = GyroLog(
        times_s=times,
        gyro=gyro.values,
        accel=accel,
        start_epoch_s=None,
        orientation="XYZ",  # Android's own device axes
        header={"source": "recorded in the .mcraw"},
        clock="video",
    )
    first_row = camera.first_row_s if camera is not None else 0.0
    frames = np.asarray((stamps - start) / 1e9 + first_row, dtype=np.float64)
    ois = None
    if motion.ois is not None:
        ois = Samples(motion.ois.times_ns - start, motion.ois.values)
    return RawGyro(log, frames, "mcraw", camera, ois)


def with_lens_shift(log: GyroLog, ois: Samples, axes: Axes, focal_px: float, scale: float) -> GyroLog:
    """The log with the lens shifts of optical image stabilisation added as the small turn that moves the
    picture as far (shift / focal length, in radians). Stabilisation models the picture as moved only by the
    camera turning; with OIS on, the lens also moves it, cancelling most of the shake, so the gyro alone puts
    back shake the lens had already removed. `ois` times are nanoseconds on the log's clock; `scale` takes
    lens-shift pixels to the recorded frame's."""
    if len(ois.times_ns) < 2 or len(log.times_s) < 2 or focal_px <= 0:
        return log
    moments = ois.times_ns / 1e9
    angle = np.stack(
        [np.interp(log.times_s, moments, ois.values[:, k] * scale / focal_px) for k in range(2)], -1
    )
    rate = np.gradient(angle, log.times_s, axis=0)
    gyro = np.array(log.gyro, dtype=np.float64, copy=True)
    for k in range(2):  # picture x, then y: the same device axes and signs that move it
        gyro[:, axes.axes[k]] += axes.signs[k] * rate[:, k]
    return replace(log, gyro=gyro)
