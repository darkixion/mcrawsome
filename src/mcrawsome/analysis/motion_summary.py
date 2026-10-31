"""What the gyro and accelerometer say about a clip's movement, without decoding a picture.

Facts and suggestions only: no shot-quality scores and no tripod claims. The intended path is the
angular rate's 0.5 s moving average, the slow movement a stabiliser follows; shake is what is left.
Blur is the shake swept during each exposure times the focal length in pixels, so a steady pan's own
blur is not counted. It is approximate: rotation about the lens axis blurs the corners less.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import numpy as np
import numpy.typing as npt
from pydantic import BaseModel, Field

from mcrawsome.io.gyro import GyroLog

Array = npt.NDArray[np.float64]
Movement = Literal["still", "pan", "moving", "handheld"]

RATE = 200.0  # Hz: logs are resampled to an even grid
PATH_S = 0.5  # the intended path's moving-average window
STILL_DEG_S = 1.5  # rms rotation below this, with little vertical acceleration, is Still
STILL_M_S2 = 0.15
PAN_DEG_S = 4.0  # a steady turn at least this fast, in one direction, is a pan or tilt
MOVING_M_S2 = 0.32  # up-and-down acceleration (1 to 4 Hz) this strong means the phone is carried along
JOLT_DEG_S = 20.0  # a handling jolt reaches at least this, and three times the clip's 90th percentile
EDGE_S = 1.5  # jolts are looked for this close to either end
MOST_TRIMMED_S = 2.0
FOCUS_SETTLED = 0.15  # dioptres: focus stays within this for a second once it has settled
BLURRED_PX = 20.0  # shake blur worth a review marker and a Stabilise note

MOVEMENT_LABELS: dict[Movement, str] = {
    "still": "Still",
    "pan": "Pan or tilt",
    "moving": "Moving",
    "handheld": "Handheld",
}
MOVEMENT_HELP: dict[Movement, str] = {
    "still": "Almost no rotation or travel, as on a tripod or a steady support",
    "pan": "A steady turn in one direction (a pan or tilt)",
    "moving": "Strong up-and-down movement: carried while walking or travelling",
    "handheld": "Ordinary handheld movement",
}


@dataclass(frozen=True)
class Rates:
    """A gyro log on an even grid, in seconds on the log's own clock."""

    times: Array
    rotation: Array  # (n, 3) rad/s
    total: Array  # degrees/s
    shake: Array  # degrees/s: the rate minus its intended path
    vertical: Array | None  # m/s2: up-and-down acceleration between 1 and 4 Hz
    holes: Array  # (k, 2) stretches the log does not cover


class TrimSuggestion(BaseModel):
    start_s: float = Field(description="Suggested mark in, seconds after the first frame starts")
    end_s: float = Field(description="Suggested mark out (exclusive), seconds after the first frame starts")
    reasons: list[str] = Field(default_factory=list, description="Why each end moved; empty when neither did")


class MotionSummary(BaseModel):
    movement: Movement | None = Field(None, description="The movement covering most of the clip")
    shares: dict[Movement, float] = Field(default_factory=dict, description="Share of seconds per movement")


def rates(log: GyroLog, start: float, end: float) -> Rates:
    """Resample the part of `log` around [start, end] (a second either side) to RATE."""
    from scipy.ndimage import uniform_filter1d  # imported here: library scans only need Movement

    t = log.times_s
    if (
        len(t) < 4
        or log.gyro.shape != (len(t), 3)
        or not np.isfinite(t).all()
        or not np.isfinite(log.gyro).all()
        or np.any(np.diff(t) <= 0)
        or (log.accel is not None and (log.accel.shape != log.gyro.shape or not np.isfinite(log.accel).all()))
    ):
        raise ValueError("Gyro samples must be finite with increasing timestamps")
    lo, hi = max(start - 1, float(t[0])), min(end + 1, float(t[-1]))
    if hi - lo < 4 / RATE:
        raise ValueError("The gyro log does not cover the clip")
    grid = np.arange(lo, hi, 1 / RATE)
    rotation = np.stack([np.interp(grid, t, log.gyro[:, k]) for k in range(3)], -1)
    size = max(1, round(PATH_S * RATE))
    path = uniform_filter1d(rotation, size=size, axis=0, mode="nearest")
    vertical = None
    if log.accel is not None:
        accel = np.stack([np.interp(grid, t, log.accel[:, k]) for k in range(3)], -1)
        gravity = uniform_filter1d(accel, size=max(1, round(2 * RATE)), axis=0, mode="nearest")
        norm = np.linalg.norm(gravity, axis=1, keepdims=True)
        up = np.divide(gravity, norm, out=np.zeros_like(gravity), where=norm > 0)
        along = np.einsum("ij,ij->i", accel - gravity, up)
        band = along - uniform_filter1d(along, size=max(1, round(RATE)), mode="nearest")
        vertical = uniform_filter1d(band, size=max(1, round(0.08 * RATE)), mode="nearest")
    return Rates(
        times=grid,
        rotation=rotation,
        total=np.degrees(np.linalg.norm(rotation, axis=1)),
        shake=np.degrees(np.linalg.norm(rotation - path, axis=1)),
        vertical=vertical,
        holes=log.holes(),
    )


def _covered(r: Rates, a: Array, b: Array) -> npt.NDArray[np.bool_]:
    inside: npt.NDArray[np.bool_] = (a >= r.times[0]) & (b <= r.times[-1])
    for first, last in r.holes:
        inside &= (b <= first) | (a >= last)
    return inside


def frame_blur(r: Rates, centres: Array, exposure_s: Array | float, focal_px: float) -> Array:
    """Shake blur per frame in pixels (NaN where the log does not cover the exposure)."""
    centres = np.asarray(centres, dtype=np.float64)
    exposure = np.broadcast_to(np.asarray(exposure_s, dtype=np.float64), centres.shape)
    swept = np.r_[0.0, np.cumsum(np.radians(r.shake[:-1]) * np.diff(r.times))]
    a, b = centres - exposure / 2, centres + exposure / 2
    blur = (np.interp(b, r.times, swept) - np.interp(a, r.times, swept)) * focal_px
    return np.where(_covered(r, a, b), blur, np.nan)


def movement(r: Rates, start: float, end: float) -> MotionSummary:
    """Each whole second of [start, end] is Still, Pan or tilt, Moving or Handheld, in that order."""
    counts: dict[Movement, int] = {"still": 0, "pan": 0, "moving": 0, "handheld": 0}
    for second in np.arange(start, end - 0.5, 1.0):
        window = (r.times >= second) & (r.times < second + 1)
        if window.sum() < RATE / 2 or not _covered(r, np.array([second]), np.array([second + 1]))[0]:
            continue
        rotation, total = r.rotation[window], r.total[window]
        rms = float(np.sqrt(np.mean(total**2)))
        mean = float(np.degrees(np.linalg.norm(rotation.mean(0))))
        spread = float(np.degrees(np.sqrt(np.mean(np.sum((rotation - rotation.mean(0)) ** 2, axis=1)))))
        level = None
        if r.vertical is not None:
            around = (r.times >= second - 0.5) & (r.times < second + 1.5)
            level = float(np.sqrt(np.mean(r.vertical[around] ** 2)))
        kind: Movement = "handheld"
        if rms < STILL_DEG_S and (level is None or level < STILL_M_S2):
            kind = "still"
        elif mean >= PAN_DEG_S and spread < 0.5 * rms:
            kind = "pan"
        elif level is not None and level >= MOVING_M_S2:
            kind = "moving"
        counts[kind] += 1
    seconds = sum(counts.values())
    if seconds == 0:
        return MotionSummary()
    shares: dict[Movement, float] = {kind: round(count / seconds, 3) for kind, count in counts.items()}
    return MotionSummary(movement=max(shares, key=lambda kind: shares[kind]), shares=shares)


def suggest_trim(
    r: Rates, first_start: float, duration_s: float, focus: Array | None = None, frame_rate: float = 24.0
) -> TrimSuggestion:
    """Marks that leave out the jolt of pressing record or stop, and focus still settling at the start.

    `first_start` is the first frame's start on the log's clock and `duration_s` the clip's length;
    `focus` holds each frame's focus distance in dioptres (NaN where not recorded). At most two seconds
    go from either end and at least half of the clip stays.
    """
    end = first_start + duration_s
    keep = TrimSuggestion(start_s=0.0, end_s=round(duration_s, 3))
    if duration_s < 4:
        return keep
    inside = (r.times >= first_start) & (r.times <= end)
    middle = inside & (r.times > first_start + EDGE_S) & (r.times < end - EDGE_S)
    if middle.sum() < RATE:
        return keep
    typical = float(np.percentile(r.total[middle], 90))
    jolt, calm = max(3 * typical, JOLT_DEG_S), max(1.5 * typical, 0.6 * JOLT_DEG_S)
    start_s, end_s, starting, ending = 0.0, duration_s, [], []
    head = inside & (r.times <= first_start + EDGE_S)
    if head.any() and r.total[head].max() >= jolt:
        last = float(r.times[head][r.total[head] >= calm].max())
        start_s = min(MOST_TRIMMED_S, last - first_start + 0.1)
        peak = float(r.total[head].max())
        starting.append(f"Start: a handling jolt (up to {peak:.0f} degrees/s) until {start_s:.1f} s")
    tail = inside & (r.times >= end - EDGE_S)
    if tail.any() and r.total[tail].max() >= jolt:
        first = float(r.times[tail][r.total[tail] >= calm].min())
        end_s = max(duration_s - MOST_TRIMMED_S, first - first_start - 0.1)
        peak = float(r.total[tail].max())
        ending.append(
            f"End: a handling jolt (up to {peak:.0f} degrees/s) from {end_s:.1f} s, usually pressing stop"
        )
    if focus is not None and len(focus) > frame_rate:
        settle = _focus_settles(np.asarray(focus, dtype=np.float64), frame_rate)
        if settle is not None and settle / frame_rate > start_s and settle / frame_rate <= MOST_TRIMMED_S:
            start_s = settle / frame_rate
            starting = [f"Start: focus is still moving until {start_s:.1f} s"]
    if end_s - start_s < duration_s / 2:
        return keep
    reasons = starting + ending
    return TrimSuggestion(start_s=round(start_s, 3), end_s=round(end_s, 3), reasons=reasons)


def _focus_settles(focus: Array, frame_rate: float) -> int | None:
    """The first frame from which focus stays within FOCUS_SETTLED dioptres for a second, when that is
    not the first frame; None when focus is not recorded or never settles in the first two seconds."""
    span = max(2, round(frame_rate))
    for index in range(min(len(focus) - span, round(MOST_TRIMMED_S * frame_rate)) + 1):
        window = focus[index : index + span]
        if not np.isfinite(window).all():
            return None
        if float(window.max() - window.min()) < FOCUS_SETTLED:
            return index or None
    return None


def steadiest_frame(blur: Array, focus: Array | None = None) -> int | None:
    """The frame in the middle half with the least shake blur, avoiding frames where focus moves."""
    blur = np.asarray(blur, dtype=np.float64)
    count = len(blur)
    if count == 0:
        return None
    lo, hi = count // 4, max(count // 4 + 1, (3 * count) // 4)
    candidates = blur[lo:hi].copy()
    if focus is not None and len(focus) == count:
        moving = np.abs(np.gradient(np.asarray(focus, dtype=np.float64)))[lo:hi] > 0.05
        candidates[moving] = np.nan
    if not np.isfinite(candidates).any():
        return None
    return lo + int(np.nanargmin(candidates))


def blurred_stretches(blur: Array, frame_rate: float, minimum_s: float = 0.5) -> list[tuple[int, int, float]]:
    """(first frame, frame count, peak pixels) of stretches whose shake blur stays above BLURRED_PX."""
    mask = np.nan_to_num(np.asarray(blur, dtype=np.float64), nan=0.0) >= BLURRED_PX
    edges = np.diff(np.r_[False, mask, False].astype(np.int8))
    result = []
    for first, last in zip(np.flatnonzero(edges == 1), np.flatnonzero(edges == -1), strict=True):
        if last - first >= max(1, round(frame_rate * minimum_s)):
            result.append((int(first), int(last - first), float(np.nanmax(blur[first:last]))))
    return result
