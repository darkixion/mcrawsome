"""Conservative, bounded gravity check of a RAW clip's initial display orientation.

Gyro rates cannot establish absolute up. Acceleration includes gravity and translation;
only stable near-gravity samples with a known camera-axis mapping can suggest a turn.
This is advice, never a change to the source, recipe or stabilisation axis mapping.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import numpy as np
from pydantic import BaseModel, ConfigDict

from mcrawsome.camera.profile import Axes
from mcrawsome.io.gcsv import GcsvError, read_gcsv
from mcrawsome.io.gyro import GyroLog

PREFIX_BYTES = 256 * 1024
PREFIX_SECONDS = 15.0  # of recorded motion: about as long as PREFIX_BYTES of a 400 Hz .gcsv
_GRAVITY = 9.80665


class OrientationHint(BaseModel):
    model_config = ConfigDict(frozen=True)

    appears_wrong: bool
    suggested_rotation: int  # absolute develop.rotation, relative to As recorded
    confidence: float  # share of reliable initial samples agreeing with this turn
    sampled_seconds: float
    message: str


def estimate(log: GyroLog, axes: Axes, recorded: int, rotation: int = 0) -> OrientationHint | None:
    if log.accel is None or log.orientation != "XYZ" or recorded not in (0, 1, 2, 3):
        return None
    if rotation not in (0, 90, 180, 270) or len(log.times_s) < 50:
        return None
    if log.accel.shape != (len(log.times_s), 3) or log.gyro.shape != log.accel.shape:
        return None
    if sorted(axes.axes) != [0, 1, 2] or any(s not in (-1, 1) for s in axes.signs):
        return None
    basis = np.eye(3)[list(axes.axes)] * np.array(axes.signs)[:, None]
    if not np.isclose(np.linalg.det(basis), 1):
        return None  # a reflection is not a physical device-to-camera rotation
    finite = np.isfinite(log.times_s) & np.isfinite(log.accel).all(1) & np.isfinite(log.gyro).all(1)
    if not finite.all() or np.any(np.diff(log.times_s) <= 0):
        return None
    # Inspect at most the first five seconds. Do not infer orientation while the phone
    # is rotating quickly, accelerating hard, or pointed almost straight up/down.
    early = log.times_s - log.times_s[0] <= 5
    force = np.linalg.norm(log.accel, axis=1)
    usable = early & (abs(force - _GRAVITY) < 0.12 * _GRAVITY)
    usable &= np.linalg.norm(log.gyro, axis=1) < 0.35
    if usable.sum() < 50 or usable.sum() < early.sum() * 0.8:
        return None
    t = log.times_s[usable]
    if t[-1] - t[0] < 1 or np.max(np.diff(t)) > 0.25:
        return None
    up = log.accel[usable] @ basis.T
    if recorded == 3:
        up[:, :2] *= -1
    projected = np.linalg.norm(up[:, :2], axis=1)
    if np.quantile(projected / force[usable], 0.1) < 0.65:
        return None
    # Pixel coordinates have y down. The angle turning the acceleration/up vector
    # towards screen-up is clockwise atan2(-x, -y).
    angles = np.degrees(np.arctan2(-up[:, 0], -up[:, 1])) % 360
    turns = np.array([0, 90, 180, 270])
    differences = (angles[:, None] - turns + 180) % 360 - 180
    agreement = (abs(differences) < 20).mean(0)
    best = int(np.argmax(agreement))
    confidence = float(agreement[best])
    if confidence < 0.9:
        return None
    suggested = int(turns[best])
    wrong = suggested != rotation
    label = {
        0: "As recorded",
        90: "Rotate 90° clockwise",
        180: "Rotate 180°",
        270: "Rotate 90° anticlockwise",
    }
    message = (
        f"Gravity near the start suggests '{label[suggested]}'. "
        "Review the preview: intentional tilts or vehicle acceleration can affect this estimate."
    )
    if wrong:
        message = "Orientation appears wrong. " + message
    return OrientationHint(
        appears_wrong=wrong,
        suggested_rotation=suggested,
        confidence=confidence,
        sampled_seconds=round(float(t[-1] - t[0]), 2),
        message=message,
    )


@lru_cache(maxsize=256)
def _prefix(path: Path, stamp: tuple[int, int, int, int]) -> GyroLog:
    log = read_gcsv(path, max_bytes=PREFIX_BYTES)
    stat = path.stat()
    if (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns) != stamp:
        raise ValueError("Gyro log changed during orientation check")
    return log


@lru_cache(maxsize=512)
def _assessment(
    path: Path, stamp: tuple[int, int, int, int], axes: Axes, recorded: int, rotation: int
) -> OrientationHint | None:
    return estimate(_prefix(path, stamp), axes, recorded, rotation)


@lru_cache(maxsize=256)
def _recorded_prefix(path: Path, stamp: tuple[int, int, int, int]) -> GyroLog | None:
    """The first PREFIX_SECONDS of the motion recorded in a RAW clip (MotionCam 5.1 and later)."""
    from mcrawsome.io.clip import Clip
    from mcrawsome.motion.raw_gyro import raw_gyro

    with Clip(path, metadata_only=True) as clip:
        source = raw_gyro(clip)
        if source is None or not source.recorded:
            return None
        clip.check_unchanged()
    log = source.log
    keep = log.times_s <= log.times_s[0] + PREFIX_SECONDS
    accel = log.accel[keep] if log.accel is not None else None
    return GyroLog(log.times_s[keep], log.gyro[keep], accel, None, log.orientation, log.header, log.clock)


def check_recorded(path: Path, axes: Axes, recorded: int | None, rotation: int = 0) -> OrientationHint | None:
    """The check from the motion recorded in a RAW clip, with the gravity axes its calibration gives."""
    from mcrawsome.io.container import ContainerError
    from mcrawsome.motion.raw_gyro import GyroError

    if recorded is None:
        return None
    try:
        stat = path.stat()
        log = _recorded_prefix(path, (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns))
        return estimate(log, axes, recorded, rotation) if log is not None else None
    except (GyroError, ContainerError, OSError, ValueError, KeyError, IndexError):
        return None  # unavailable evidence must not make a playable clip unreadable


def check(path: Path, axes: Axes | None, recorded: int | None, rotation: int = 0) -> OrientationHint | None:
    if axes is None or recorded is None:
        return None
    try:
        stat = path.stat()
        stamp = stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns
        return _assessment(path, stamp, axes, recorded, rotation)
    except (GcsvError, OSError, ValueError, KeyError, IndexError):
        return None  # unavailable evidence must not make a playable clip unreadable
