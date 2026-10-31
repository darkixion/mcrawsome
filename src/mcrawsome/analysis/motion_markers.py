"""Conservative motion suggestions from sensors alone. No shot-quality or tripod claims."""

from __future__ import annotations

from collections.abc import Callable
from itertools import pairwise

import numpy as np
import numpy.typing as npt

from mcrawsome.analysis.review_markers import REVIEW_COLOURS
from mcrawsome.io.gyro import MAX_GAP_S, GyroLog
from mcrawsome.resolve.markers import Marker, MarkerColour


def suggest(
    log: GyroLog, centres: npt.NDArray[np.float64], rate: float, check: Callable[[], None] = lambda: None
) -> list[Marker]:
    check()
    t, gyro, accel = log.times_s, log.gyro, log.accel
    if accel is None or len(t) < 4:
        raise ValueError("Motion markers need gyro and accelerometer samples")
    if (
        gyro.shape != (len(t), 3)
        or accel.shape != gyro.shape
        or not np.isfinite(gyro).all()
        or not np.isfinite(accel).all()
        or not np.isfinite(t).all()
        or np.any(np.diff(t) <= 0)
        or not np.isfinite(centres).all()
        or not np.isfinite(rate)
        or rate <= 0
    ):
        raise ValueError("Invalid sensor timing or samples for motion markers")
    # Smooth sensor noise before differentiating. Derivatives use actual, potentially irregular times.
    radius = min(len(t) // 4, max(1, round(0.015 / float(np.median(np.diff(t))))))
    kernel = np.ones(2 * radius + 1) / (2 * radius + 1)
    smooth = np.stack(
        [np.convolve(np.pad(accel[:, k], radius, mode="edge"), kernel, "valid") for k in range(3)], -1
    )
    speed = np.linalg.norm(gyro, axis=1)
    force = np.linalg.norm(smooth, axis=1)
    derivative = np.diff(smooth, axis=0) / np.diff(t)[:, None]
    # Changing gravity direction during rotation is not itself a bump. Subtract a conservative bound
    # for its derivative; classify only the excess. This does not attempt to integrate translation.
    jerk = np.r_[0.0, np.maximum(0, np.linalg.norm(derivative, axis=1) - speed[1:] * 9.80665)]
    jerk[1:][np.diff(t) > MAX_GAP_S] = 0
    bins = np.floor((t - t[0]) / 0.25).astype(np.int64)
    breaks = np.r_[0, np.flatnonzero(np.diff(bins)) + 1, len(t)]
    labels: dict[int, str] = {}
    for first, last in pairwise(breaks):
        check()
        if last - first < 4 or t[last - 1] - t[first] < 0.1:
            continue
        sl = slice(first, last)
        mean = gyro[sl].mean(0)
        spread = float(np.sqrt(np.mean(np.sum((gyro[sl] - mean) ** 2, axis=1))))
        rms = float(np.sqrt(np.mean(speed[sl] ** 2)))
        bump = float(np.quantile(jerk[sl], 0.95))
        label = ""
        if bump > 30 or spread > np.radians(40) or np.max(abs(force[sl] - 9.80665)) > 0.9 * 9.80665:
            label = "shake"
        elif rms < np.radians(1.5) and np.std(force[sl]) < 0.04 * 9.80665 and bump < 3:
            label = "low"
        elif np.radians(1.5) <= rms < np.radians(90) and spread < 0.35 * rms and bump < 4:
            label = "steady"
        labels[int(bins[first])] = label
    categories = [
        labels.get(int(np.floor((c - t[0]) / 0.25)), "") if t[0] <= c <= t[-1] else "" for c in centres
    ]
    for start, end in log.holes():
        for i in np.flatnonzero((centres > start) & (centres < end)):
            categories[int(i)] = ""
    result = []
    first = 0
    for last in range(1, len(categories) + 1):
        if last < len(categories) and categories[last] == categories[first]:
            continue
        kind = categories[first]
        minimum = 0.75 if kind == "shake" else 1.0
        if kind and last - first >= max(1, round(rate * minimum)):
            descriptions: dict[str, tuple[MarkerColour, str]] = {
                "low": ("Green", "Low motion"),
                "steady": ("Blue", "Steady rotation"),
                "shake": (REVIEW_COLOURS["Possible shake"], "Possible shake"),
            }
            colour, name = descriptions[kind]
            result.append(
                Marker(
                    first,
                    last - first,
                    colour,
                    name,
                    "MCRAWsome sensor-based suggestion; review the picture.",
                )
            )
        first = last
    return result
