"""Bounded envelopes of recorded IMU samples for the clip details pane."""

import threading
from pathlib import Path

import numpy as np
import numpy.typing as npt
from pydantic import BaseModel

from mcrawsome.analysis.diagnostics import _check
from mcrawsome.camera.profile import ProfileError
from mcrawsome.errors import McError
from mcrawsome.io.directlog import probe_direct_log, read_gyro
from mcrawsome.io.gcsv import GcsvError, read_gcsv
from mcrawsome.io.gyro import GyroLog
from mcrawsome.output.ffmpeg import find_ffmpeg

MAX_LOG_BYTES = 32 << 20
MAX_BUCKETS = 256


class GyroBucket(BaseModel):
    frame: int | None = None
    start_s: float
    end_s: float
    minimum: list[float]
    maximum: list[float]
    mean: list[float]


class GyroPlot(BaseModel):
    samples: int
    duration_s: float
    sample_rate_hz: float | None
    angular_velocity: list[GyroBucket]
    acceleration: list[GyroBucket] | None
    axes: str = "Recorded device X, Y, Z"
    time_origin: str = "Seconds since first IMU sample"
    frame_note: str | None = None


def envelopes(log: GyroLog) -> GyroPlot:
    times = log.times_s
    if (
        len(times) < 1
        or log.gyro.shape != (len(times), 3)
        or not np.isfinite(times).all()
        or not np.isfinite(log.gyro).all()
        or np.any(np.diff(times) <= 0)
        or (log.accel is not None and (log.accel.shape != log.gyro.shape or not np.isfinite(log.accel).all()))
    ):
        raise ValueError("Gyro samples must be finite with increasing timestamps")
    relative = times - times[0]
    groups = np.array_split(np.arange(len(times)), min(MAX_BUCKETS, len(times)))

    def series(values: npt.NDArray[np.float64]) -> list[GyroBucket]:
        return [
            GyroBucket(
                start_s=float(relative[group[0]]),
                end_s=float(relative[group[-1]]),
                minimum=values[group].min(axis=0).tolist(),
                maximum=values[group].max(axis=0).tolist(),
                mean=values[group].mean(axis=0).tolist(),
            )
            for group in groups
        ]

    return GyroPlot(
        samples=len(times),
        duration_s=float(relative[-1]),
        sample_rate_hz=float(1 / np.median(np.diff(times))) if len(times) > 1 else None,
        angular_velocity=series(np.rad2deg(log.gyro)),
        acceleration=series(log.accel) if log.accel is not None else None,
    )


def measure_gyro(path: Path, stop: threading.Event | None = None) -> GyroPlot:
    _check(stop)
    if path.suffix.lower() == ".mcraw" and (recorded := _recorded(path, stop)) is not None:
        log, frame_times = recorded
        report = envelopes(log)
        try:
            attach_frames(report, frame_times - log.times_s[0])
            report.frame_note = (
                "Recorded in the clip on its frames' own sensor clock; the nearest recorded frame uses "
                "mid-exposure/readout. Time starts at the first IMU sample; samples before the first frame "
                "or after the last have no frame."
            )
        except ValueError as exc:
            report.frame_note = f"Recorded frame link unavailable: {exc}. IMU measurements remain valid."
        return report
    if path.suffix.lower() == ".mcraw":
        log_path = path.with_suffix(".gcsv")
        if not log_path.is_file():
            raise McError("No matching MotionCam gyro log", code="input-unreadable")
        stamp = log_path.stat()
        if stamp.st_size > MAX_LOG_BYTES:
            raise McError("Gyro graph supports logs up to 32 MiB", code="input-unreadable")
        try:
            log = read_gcsv(log_path, max_bytes=MAX_LOG_BYTES + 1)
        except (GcsvError, ValueError, KeyError, IndexError) as exc:
            raise ValueError(f"Cannot read gyro samples: {exc}") from None
        after = log_path.stat()
        if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != (
            stamp.st_dev,
            stamp.st_ino,
            stamp.st_size,
            stamp.st_mtime_ns,
        ):
            raise ValueError("Gyro log changed while reading")
    else:
        ffmpeg, ffprobe = find_ffmpeg()
        embedded, _ = read_gyro(ffmpeg, probe_direct_log(ffprobe, path))
        if embedded is None:
            raise ValueError("No readable embedded gyro samples")
        log = embedded
    _check(stop)
    report = envelopes(log)
    try:
        frame_times, note = recording_clock(path, log, stop)
        attach_frames(report, frame_times - log.times_s[0])
        report.frame_note = note
    except (ValueError, OSError, McError, ProfileError) as exc:
        report.frame_note = f"Recorded frame link unavailable: {exc}. IMU measurements remain valid."
    return report


def _recorded(path: Path, stop: threading.Event | None) -> tuple[GyroLog, npt.NDArray[np.float64]] | None:
    """The motion recorded in a RAW clip (MotionCam 5.1 and later) and its frames' exposure centres on the
    same clock; None for a clip without recorded motion."""
    from mcrawsome.analysis.frame_values import frame_timing
    from mcrawsome.io.clip import Clip
    from mcrawsome.io.container import ContainerError
    from mcrawsome.library.scan import clip_recipe
    from mcrawsome.motion.raw_gyro import GyroError, has_recorded_motion, raw_gyro
    from mcrawsome.pipeline.stabilise import camera_profile
    from mcrawsome.recipe.clip_layer import read_layer

    if not has_recorded_motion(path):  # unreadable or without it: the .gcsv beside it, if any
        return None
    with Clip(path, metadata_only=True) as clip:
        try:
            source = raw_gyro(clip)
        except (GyroError, ContainerError) as exc:
            raise ValueError(f"Cannot read the recorded motion: {exc}") from None
        if source is None:
            return None
        rows = frame_timing(clip, stop)
        exposure = np.array(
            [float(row.exposure) if row is not None and row.exposure is not None else np.nan for row in rows],
            dtype=np.float64,
        )
        profile = camera_profile(clip, clip_recipe(path, read_layer(path)))
        readout_ms = profile.readout_ms if profile.readout_ms is not None else 0.4 / clip.fps * 1000
        clip.check_unchanged()
    exposure = np.where(np.isfinite(exposure), exposure, np.nanmedian(exposure))
    return source.log, source.frame_times_s + exposure / 2e9 + readout_ms / 2000


def attach_frames(report: GyroPlot, times: npt.NDArray[np.float64]) -> None:
    """Nearest recorded frame to each group midpoint; no nominal-FPS approximation."""
    if not len(times) or not np.isfinite(times).all() or np.any(np.diff(times) <= 0):
        raise ValueError("Frame timestamps must be finite and strictly increasing")
    for groups in (report.angular_velocity, report.acceleration or []):
        for group in groups:
            middle = (group.start_s + group.end_s) / 2
            if not times[0] <= middle <= times[-1]:
                group.frame = None
                continue
            right = int(np.searchsorted(times, middle))
            group.frame = right - 1 if right and middle - times[right - 1] <= times[right] - middle else right


def recording_clock(
    path: Path, log: GyroLog, stop: threading.Event | None
) -> tuple[npt.NDArray[np.float64], str]:
    from mcrawsome.io.clip import Clip
    from mcrawsome.io.container import ContainerError
    from mcrawsome.io.movie_metadata import read_movie_metadata
    from mcrawsome.library.scan import clip_recipe
    from mcrawsome.motion.sync import raw_frame_times
    from mcrawsome.pipeline.stabilise import camera_profile
    from mcrawsome.recipe.clip_layer import read_layer

    recipe = clip_recipe(path, read_layer(path))
    if path.suffix.lower() != ".mcraw":
        if log.clock != "video":
            raise ValueError("Telemetry has no video-clock link")
        movie = read_movie_metadata(path)
        times = movie.times_ns.astype(np.float64) / 1e9 + (recipe.camera.sync_ms or 0) / 1000
        return times, "Nearest recorded frame uses indexed movie timestamps and the saved sync offset."
    from mcrawsome.analysis.frame_values import frame_timing

    try:
        with Clip(path, metadata_only=True) as clip:
            rows = frame_timing(clip, stop)
            if any(row is None for row in rows):
                raise ValueError("Frame metadata cannot be read for every frame")
            received = [float(row.received_ms) for row in rows if row is not None]
            exposure = [float(row.exposure) for row in rows if row is not None]
            if not np.isfinite(received).all() or not np.isfinite(exposure).all():
                raise ValueError("Frame receive/exposure timestamps are incomplete")
            profile = camera_profile(clip, recipe)
            times = raw_frame_times(clip.timestamps_ns, np.asarray(received), log)
            readout_ms = profile.readout_ms if profile.readout_ms is not None else 0.4 / clip.fps * 1000
            times += np.asarray(exposure) / 2e9 + readout_ms / 2000 + (profile.gcsv_sync_ms or 0) / 1000
            clip.check_unchanged()
            return times, (
                "Nearest recorded frame uses receive-clock alignment, mid-exposure/readout and "
                "the saved camera/profile sync offset. This is not a visual sync fit. "
                "Time starts at the first IMU sample; unlinked lead-in/out has no frame."
            )
    except (ContainerError, KeyError, TypeError) as exc:
        raise ValueError(f"Cannot link frame metadata ({exc})") from None
