"""Sync the gyro to the picture: time offset, axis mapping and focal length, with a confidence.

Port of the fit in the prototype's stabilise.py. The image motion measured between consecutive frames
is compared with the gyro's rotation between the same frames' mid-exposure times:

1. Coarse sync, axis-free: correlate image speed with gyro rotation speed over a search window.
2. Fine sync: at each offset near the coarse one, fit the mapping from gyro rotation to image motion by
   least squares and keep the offset with the smallest error. With a camera profile that knows the axes
   (a constrained fit), only one overall scale is fitted, which is far more robust when the measured
   motion is poor (moving subjects, close-ups).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

import numpy as np
import numpy.typing as npt

from mcrawsome.camera.profile import Axes, CameraProfile
from mcrawsome.io.gyro import GyroLog
from mcrawsome.motion.flow import MotionTrack
from mcrawsome.motion.orientation import AxisMapping

Array = npt.NDArray[np.float64]
GyroKind = Literal["gcsv", "gpmf", "mcraw"]  # "mcraw": recorded in the clip, on its frames' clock

MIN_CONFIDENCE = 0.6
_STILL_PX = 0.7  # below this RMS image speed per frame, the picture cannot be trusted for sync
_AMBIGUOUS = 1.15
_TIMING_TOLERANCE_S = 0.025  # a fit this close to the profile's sync is the profile's sync
_MIN_SHAKE_REMOVED = 0.25
_NOISE_PX = 0.5  # frame-to-frame shake below this is mostly measurement noise


@dataclass(frozen=True)
class SyncResult:
    offset_s: float
    axes: AxisMapping
    focal_px: float
    confidence: float  # share of the image motion the gyro explains (R squared of dx and dy)
    correlation: tuple[float, float, float]
    peak_ratio: float
    constrained: bool
    reason: str | None  # why it should not be trusted, when it should not
    note: str | None = None  # how it came to be trusted, when the picture alone would not have
    gains: tuple[float, float, float] = (1.0, 1.0, 1.0)  # correction kept side to side, up and down, roll

    @property
    def confident(self) -> bool:
        return self.reason is None


def cumulative_rotation(log: GyroLog) -> Array:
    """Integrated rotation (rad) per device axis at each sample time (trapezoid rule)."""
    steps = 0.5 * (log.gyro[1:] + log.gyro[:-1]) * np.diff(log.times_s)[:, None]
    return np.asarray(np.concatenate([np.zeros((1, 3)), np.cumsum(steps, 0)]), dtype=np.float64)


def turning_window(log: GyroLog, frame_times_s: Array, length: int) -> int:
    """The first of `length` consecutive frames over which the camera turns most (the gyro's angle between
    frames, summed). The picture confirms the gyro timing best where rotation, rather than the camera's own
    travel or moving subjects, moves it."""
    count = len(frame_times_s)
    length = max(1, min(length, count))
    if length == count or count < 2:
        return 0
    cum = cumulative_rotation(log)
    turns = np.linalg.norm(_rotations(cum, log.times_s, frame_times_s[:-1], frame_times_s[1:]), axis=1)
    sums = np.convolve(turns, np.ones(length - 1), "valid") if length > 1 else turns
    return int(np.argmax(sums))


def raw_frame_times(timestamps_ns: npt.NDArray[np.int64], received_ms: Array, log: GyroLog) -> Array:
    """Frame start times on a .gcsv log's clock, linked through the smallest receive latency."""
    if log.start_epoch_s is None:
        raise ValueError("the gyro log has no start time, so it cannot be linked to the frames")
    offset = (np.asarray(received_ms, dtype=np.int64) * 1_000_000 - timestamps_ns).min()
    # Subtract the epoch as integers before converting to seconds. Subtracting two
    # large float timestamps discards sub-microsecond frame timing precision.
    anchor = int(timestamps_ns[0]) + int(offset) - round(log.start_epoch_s * 1e9)
    return np.asarray((timestamps_ns - timestamps_ns[0]) / 1e9 + anchor / 1e9, dtype=np.float64)


def _rotations(cum: Array, log_times: Array, a: Array, b: Array) -> Array:
    return np.stack(
        [np.interp(b, log_times, cum[:, k]) - np.interp(a, log_times, cum[:, k]) for k in range(3)], -1
    )


def _corr(x: Array, y: Array) -> float:
    if np.std(x) < 1e-12 or np.std(y) < 1e-12:
        return 0.0
    return float(np.corrcoef(x, y)[0, 1])


def fit_sync(
    log: GyroLog,
    frame_times_s: Array,
    exposure_s: float | Array,
    motion: MotionTrack,
    seed: CameraProfile | None,
    kind: GyroKind,
    readout_s: float,
    search_s: float | None = None,
    min_confidence: float = MIN_CONFIDENCE,
    focal_known: bool = False,
) -> SyncResult:
    """`focal_known`: the seed's focal length is a measurement (a profile, a Recipe override or the lens
    metadata), so the fit may move it by at most FOCAL_TOLERANCE; parallax and moving subjects otherwise
    pull it far from the lens on walking shots, and every correction in pixels with it."""
    cum = cumulative_rotation(log)
    first = motion.first
    meas = np.stack([motion.dx, motion.dy, motion.roll], -1)
    exposure = np.asarray(exposure_s, dtype=np.float64)
    if (
        exposure.ndim > 1
        or (exposure.ndim == 1 and exposure.shape != frame_times_s.shape)
        or not np.isfinite(exposure).all()
        or np.any(exposure < 0)
    ):
        raise ValueError("Exposure times must be finite, nonnegative and match the recorded frames")
    base = frame_times_s + exposure / 2 + readout_s / 2  # mid-exposure, mid-frame
    # Adjacent measured pairs share a boundary. Prepare the unique indices once for the whole
    # offset search, then interpolate each used boundary once per axis rather than twice.
    used, inverse = np.unique(np.concatenate([first, first + 1]), return_inverse=True)
    starts, ends = inverse[: len(first)], inverse[len(first) :]
    boundaries = base[used]

    def gyro_between(delta: float) -> Array:
        centre = boundaries + delta
        sampled = np.stack([np.interp(centre, log.times_s, cum[:, k]) for k in range(3)], -1)
        return np.asarray(sampled[ends] - sampled[starts], dtype=np.float64)

    seed_axes, seed_ms = _seed(seed, kind)
    centre_s = (seed_ms or 0.0) / 1000
    window = search_s if search_s is not None else (0.06 if seed_ms is not None else 1.5)
    # Motion recorded in the clip shares the frames' clock, axes and lens: the picture only weighs each axis.
    recorded = kind == "mcraw" and seed_axes is not None

    # 1. coarse, axis-free
    speed = np.hypot(meas[:, 0], meas[:, 1])
    coarse, peak_ratio = centre_s, 1.0
    if not recorded:
        grid = np.arange(centre_s - window, centre_s + window, 0.002)
        scores = np.array([_corr(np.linalg.norm(gyro_between(d), axis=1), speed) for d in grid])
        best = int(np.nanargmax(scores))
        coarse = float(grid[best])
        elsewhere = scores[np.abs(grid - coarse) > 0.1]
        runner_up = float(np.nanmax(elsewhere)) if len(elsewhere) else 0.0
        peak_ratio = float(scores[best] / max(runner_up, 0.05))

    # 2. fine
    scale = np.array([1000.0, 1000.0, 1.0])
    fine = np.array([coarse]) if recorded else np.arange(coarse - 0.06, coarse + 0.06, 0.0005)
    if seed_axes is not None:
        focal_seed = seed.focal_px if seed and seed.focal_px else 3000.0
        mapping = AxisMapping(seed_axes.axes, seed_axes.signs)
        tolerance = 0.0 if recorded else FOCAL_TOLERANCE if focal_known else None
        result = _constrained(gyro_between, fine, meas, mapping, focal_seed, tolerance)
        offset, focal, pred = result
        constrained = True
    else:
        offset, mapping, focal, pred = _free(gyro_between, fine, meas / scale)
        pred = pred * scale
        constrained = False

    resid = meas[:, :2] - pred[:, :2]
    total = meas[:, :2] - meas[:, :2].mean(0)
    r2 = float(1 - (resid**2).sum() / max(float((total**2).sum()), 1e-12))
    correlation = (
        _corr(pred[:, 0], meas[:, 0]),
        _corr(pred[:, 1], meas[:, 1]),
        _corr(pred[:, 2], meas[:, 2]),
    )
    checking = min_confidence > 0
    reason, disagree = None, False
    if recorded:
        pass  # the recording's own timing, axes and lens: what the picture cannot explain is not theirs
    elif checking and float(np.sqrt(np.mean(speed**2))) < _STILL_PX:
        reason = "the scene barely moves, so the picture cannot confirm the gyro timing"
    elif checking and r2 < min_confidence:
        reason = f"gyro and picture do not agree well enough (explains {max(r2, 0.0):.0%} of the motion)"
        disagree = True
    elif checking and len(set(mapping.axes)) < 3:
        reason = "the gyro axes cannot be told apart (the motion needs pans, tilts and rolls)"
    elif checking and not constrained and peak_ratio < _AMBIGUOUS:
        reason = "the gyro timing is ambiguous (two offsets fit almost equally well)"
    at_fit = gyro_between(offset)
    note = None
    if disagree and constrained and seed_ms is not None:
        focal_seed = seed.focal_px if seed and seed.focal_px else focal
        removed = shake_removed(at_fit, mapping, focal_seed, meas)
        close = abs(offset - centre_s) <= _TIMING_TOLERANCE_S
        if close and removed >= _MIN_SHAKE_REMOVED:
            reason, focal = None, focal_seed
            note = (
                "the picture moves more than the camera turns (a moving camera or moving subjects); the "
                f"gyro timing matches the camera profile and removes {removed:.0%} of the shake, so it is "
                "used with the profile's focal length"
            )
    gains = (1.0, 1.0, 1.0)
    if checking and reason is None:
        gains = axis_gains(at_fit, mapping, focal, meas)
        # Next to nothing (or a correction against the picture) is a timing or calibration error, not
        # movement the gyro cannot see: refuse, as for a wrong axis sign. Recorded motion has the recording's
        # own timing and axes, so it can only be movement the gyro cannot see: the axis is scaled instead.
        if not recorded and (against := [k for k in range(3) if gains[k] < _MIN_GAIN]):
            reason, gains = _worse_reason(against), (1.0, 1.0, 1.0)
        elif scaled := [k for k in range(3) if gains[k] < 1.0]:
            parts = ", ".join(f"{_AXIS_NAMES[k]} correction is scaled to {gains[k]:.0%}" for k in scaled)
            note = "; ".join(
                n
                for n in (
                    note,
                    f"the {parts}: all of it would add shake the gyro cannot see "
                    "(the camera's own movement, such as walking bob, or moving subjects)",
                )
                if n
            )
    return SyncResult(offset, mapping, focal, r2, correlation, peak_ratio, constrained, reason, note, gains)


def _seed(seed: CameraProfile | None, kind: GyroKind) -> tuple[Axes | None, float | None]:
    """The axes and sync (ms) a profile knows for this kind of log."""
    if seed is None:
        return None, None
    if kind == "gcsv":
        return seed.gcsv_axes, seed.gcsv_sync_ms
    if kind == "gpmf":
        return seed.gpmf_axes, seed.gpmf_sync_ms
    return seed.mcraw_axes, 0.0  # recorded on the frames' own clock


def _high(x: Array) -> Array:
    from scipy.ndimage import uniform_filter1d  # scipy loads only when needed

    return np.asarray(x - uniform_filter1d(x, 9, axis=0, mode="nearest"), dtype=np.float64)


_AXIS_NAMES = ("side-to-side", "up-and-down", "roll")
_MIN_GAIN = 0.1  # an axis that would keep less of its correction than this refuses the gyro


def _worse_reason(worse: list[int]) -> str:
    names = " and ".join(_AXIS_NAMES[k] for k in worse)
    return (
        f"the gyro correction would increase the measured {names} shake. "
        "Possible causes include gyro timing or axis calibration, parallax, and moving subjects. "
        "Changing the display orientation alone does not fix gyro-to-picture alignment"
    )


def worse_axes(gyro: Array, mapping: AxisMapping, focal: float, meas: Array) -> list[int]:
    """The axes (0 image x, 1 image y, 2 roll) whose frame-to-frame shake the gyro correction would make
    worse. Axes with almost no measured shake are left out, as their measurement is mostly noise."""
    worse = []
    for k in range(3):
        scale = focal if k < 2 else 1.0
        pred = mapping.signs[k] * gyro[:, mapping.axes[k]] * scale
        before = float(np.sqrt(np.mean(_high(meas[:, k]) ** 2)))
        after = float(np.sqrt(np.mean(_high(meas[:, k] - pred) ** 2)))
        floor = _NOISE_PX if k < 2 else _NOISE_PX / max(abs(focal), 1.0)
        if before > floor and after > before:
            worse.append(k)
    return worse


def axis_gains(gyro: Array, mapping: AxisMapping, focal: float, meas: Array) -> tuple[float, float, float]:
    """Per axis (image x, image y, roll), the share of the gyro correction to keep: all of it unless the
    full correction would add frame-to-frame shake (worse_axes), else the share that leaves the least
    (least squares, from 0 to 1)."""
    worse = worse_axes(gyro, mapping, focal, meas)
    gains = [1.0, 1.0, 1.0]
    for k in worse:
        scale = focal if k < 2 else 1.0
        pred = _high(mapping.signs[k] * gyro[:, mapping.axes[k]] * scale)
        power = float(np.dot(pred, pred))
        gains[k] = (
            round(min(1.0, max(0.0, float(np.dot(_high(meas[:, k]), pred)) / power)), 3) if power else 0.0
        )
    return gains[0], gains[1], gains[2]


def shake_removed(gyro: Array, mapping: AxisMapping, focal: float, meas: Array) -> float:
    """The share of the frame-to-frame shake (motion faster than about a third of a second) in the
    measured shift that the gyro's rotation, at this focal length, accounts for."""
    pred = np.stack([mapping.signs[k] * gyro[:, mapping.axes[k]] * focal for k in range(2)], -1)
    before = float(np.sqrt((_high(meas[:, :2]) ** 2).sum(1).mean()))
    after = float(np.sqrt((_high(meas[:, :2] - pred) ** 2).sum(1).mean()))
    return 1.0 - after / before if before > 1e-9 else 0.0


def _free(
    gyro_between: Callable[[float], Array], offsets: Array, meas: Array
) -> tuple[float, AxisMapping, float, Array]:
    best: tuple[float, float, Array, Array] | None = None
    for delta in offsets:
        g = gyro_between(float(delta))
        x = np.linalg.lstsq(g, meas, rcond=None)[0]
        error = float(((g @ x - meas) ** 2).sum())
        if best is None or error < best[0]:
            best = (error, float(delta), x, g)
    assert best is not None
    _, delta, x, g = best
    a = x.T  # rows: image dx/1000 px, dy/1000 px, roll; columns: gyro x, y, z
    ax_dx, ax_dy = int(np.argmax(np.abs(a[0]))), int(np.argmax(np.abs(a[1])))
    ax_roll = ({0, 1, 2} - {ax_dx, ax_dy}).pop() if ax_dx != ax_dy else int(np.argmax(np.abs(a[2])))
    signs = (int(np.sign(a[0, ax_dx])), int(np.sign(a[1, ax_dy])), int(np.sign(a[2, ax_roll])))
    focal = 0.5 * (abs(a[0, ax_dx]) + abs(a[1, ax_dy])) * 1000
    mapping = AxisMapping((ax_dx, ax_dy, ax_roll), signs)
    # Least squares proposes the axes and timing, but stabilisation uses one
    # signed axis per component and a shared focal length. Score that model:
    # cross-axis terms can fit parallax well without describing camera rotation.
    pred = np.stack([signs[k] * g[:, mapping.axes[k]] for k in range(3)], -1)
    pred[:, :2] *= focal / 1000  # caller's translation measurements are divided by 1000
    return delta, mapping, float(focal), pred


FOCAL_TOLERANCE = 0.25  # a known focal length may move from 0.8x to 1.25x


def _constrained(
    gyro_between: Callable[[float], Array],
    offsets: Array,
    meas: Array,
    mapping: AxisMapping,
    focal: float,
    tolerance: float | None = None,
) -> tuple[float, float, Array]:
    best: tuple[float, float, float, Array] | None = None
    for delta in offsets:
        g = gyro_between(float(delta))
        p = np.stack([mapping.signs[k] * g[:, mapping.axes[k]] for k in range(3)], -1)
        shift = p[:, :2].ravel() * focal
        k = float(shift @ meas[:, :2].ravel() / max(float(shift @ shift), 1e-12))  # one overall scale
        if tolerance is not None:
            k = min(max(k, 1 / (1 + tolerance)), 1 + tolerance)
        pred = np.column_stack([p[:, 0] * focal * k, p[:, 1] * focal * k, p[:, 2]])
        error = float(((pred[:, :2] - meas[:, :2]) ** 2).sum())
        if best is None or error < best[0]:
            best = (error, float(delta), focal * k, pred)
    assert best is not None
    return best[1], best[2], best[3]


STRIPS = ((0.0, 1 / 3), (2 / 3, 1.0))  # the top and bottom thirds of the frame, for the readout fit


def fit_readout(
    log: GyroLog,
    frame_times_s: Array,
    exposure_s: float | Array,
    top: MotionTrack,
    bottom: MotionTrack,
    seed: CameraProfile,
    kind: GyroKind,
) -> tuple[float, float]:
    """Rolling-shutter readout and the sync offset that goes with it, from the top and bottom thirds.

    A rolling shutter exposes lower rows later, so the bottom third's motion syncs later than the top
    third's by the readout times the distance between the strips' centres. Each strip is synced with a
    zero readout in a narrow window around the seed (which must know the axes and the sync); the offset
    returned is for a frame centre with the fitted readout, as `fit_sync` uses it.
    """
    offsets = [
        fit_sync(log, frame_times_s, exposure_s, track, seed, kind, 0.0, 0.03, 0.0).offset_s
        for track in (top, bottom)
    ]
    centres = [sum(rows) / 2 for rows in STRIPS]
    readout = (offsets[1] - offsets[0]) / (centres[1] - centres[0])
    return float(readout), float(offsets[0] - readout * centres[0])
