"""Gravity feedback for native timeline levelling. Heading remains gyro-relative.

Map acceleration with measured physical camera axes, then filter it in the gyro's reference frame.
This is a complementary gravity filter: rejected acceleration is bridged by gyro propagation rather
than treated as a new horizon. It never estimates position or an absolute heading.
"""

from __future__ import annotations

from collections.abc import Callable

import numpy as np
import numpy.typing as npt

from mcrawsome.camera.profile import Axes
from mcrawsome.io.gyro import MAX_GAP_S, GyroLog
from mcrawsome.motion.geometry import quat_mul, quat_to_matrix
from mcrawsome.motion.orientation import AxisMapping, Orientation

Array = npt.NDArray[np.float64]
GRAVITY = 9.80665


def reference_up(
    log: GyroLog,
    orientation: Orientation,
    axes: Axes,
    centres: Array,
    check: Callable[[], None] = lambda: None,
) -> Array:
    """Filtered camera-up in the integration reference, sampled at exposure centres.

    Require half a second of consistent, near-gravity acceleration before initialising. Feedback
    ignores force/angle outliers and holes; more than two seconds without a reliable observation
    refuses the requested lock rather than claiming a level horizon on untrusted data.
    """
    check()
    t = log.times_s
    if log.accel is None or log.orientation != "XYZ" or len(t) < 3:
        raise ValueError("Horizon Lock needs gyro and accelerometer samples in known XYZ axes")
    if sorted(axes.axes) != [0, 1, 2] or any(s not in (-1, 1) for s in axes.signs):
        raise ValueError("Horizon Lock needs a verified physical sensor-to-image mapping")
    basis = np.eye(3)[list(axes.axes)] * np.array(axes.signs)[:, None]
    if not np.isclose(np.linalg.det(basis), 1):
        raise ValueError("Horizon Lock's sensor mapping must be a physical rotation")
    acceleration = log.accel
    if (
        acceleration.shape != (len(t), 3)
        or log.gyro.shape != acceleration.shape
        or not np.isfinite(acceleration).all()
        or not np.isfinite(log.gyro).all()
        or not np.isfinite(t).all()
        or np.any(np.diff(t) <= 0)
    ):
        raise ValueError("Horizon Lock received invalid sensor samples")
    force = np.linalg.norm(acceleration, axis=1)
    usable = abs(force - GRAVITY) < 0.12 * GRAVITY
    camera = acceleration @ basis.T / np.maximum(force[:, None], 1e-12)
    # The picture fit can deliberately reduce an axis's correction (for translation/parallax).
    # Those gains are not physical angular velocity. Propagate gravity with the calibrated device
    # axes and full measured gyro, then express it in the fitted plan's reference only at the end.
    physical = Orientation.from_gyro(
        log,
        AxisMapping(
            (axes.axes[1], axes.axes[0], axes.axes[2]),
            (-axes.signs[1], axes.signs[0], -axes.signs[2]),
        ),
    )
    real = quat_to_matrix(physical.at(t))
    observed = np.einsum("nji,nj->ni", real, camera)
    # A bounded initial interval; starting during a large bump must not establish a false up.
    initial = (t <= t[0] + 1.0) & usable
    chosen = np.flatnonzero(initial)
    if len(chosen) < 20 or t[chosen[-1]] - t[chosen[0]] < 0.5:
        raise ValueError("Horizon Lock needs a stable near-gravity interval at the start")
    up = np.median(observed[initial], axis=0)
    up /= max(float(np.linalg.norm(up)), 1e-12)
    if np.quantile(observed[initial] @ up, 0.1) < np.cos(np.radians(10)):
        raise ValueError("Horizon Lock cannot establish reliable gravity near the start")
    filtered = np.empty_like(observed)
    last_good = float(t[chosen[0]])
    trusted = np.empty(len(t), dtype=bool)
    for i in range(len(t)):
        if i % 256 == 0:
            check()
        dt = float(t[i] - t[i - 1]) if i else 0.0
        if usable[i] and dt <= MAX_GAP_S and observed[i] @ up > np.cos(np.radians(15)):
            weight = -np.expm1(-dt / 2.0)
            up = (1 - weight) * up + weight * observed[i]
            up /= np.linalg.norm(up)
            last_good = float(t[i])
        filtered[i] = up
        trusted[i] = t[i] - last_good <= 2.0
    index = np.clip(np.searchsorted(t, centres, side="right") - 1, 0, len(t) - 1)
    if not orientation.covers(centres).all() or not trusted[index].all():
        raise ValueError("Horizon Lock lost reliable gravity or gyro coverage in the selected recording")
    camera_up = np.einsum("nij,nj->ni", quat_to_matrix(physical.at(centres)), filtered[index])
    return np.asarray(
        np.einsum("nji,nj->ni", quat_to_matrix(orientation.at(centres)), camera_up), dtype=np.float64
    )


def level(targets: Array, up: Array, display_rotation: int = 0) -> Array:
    """Turn each target around its optical axis so measured up points to displayed screen-up."""
    projected = np.einsum("nij,nj->ni", quat_to_matrix(targets), up)
    if np.any(np.linalg.norm(projected[:, :2], axis=1) < 0.5):
        raise ValueError("Horizon Lock is ambiguous when the camera points nearly straight up or down")
    angles = np.arctan2(-projected[:, 0], -projected[:, 1]) - np.radians(display_rotation)
    angles = (angles + np.pi) % (2 * np.pi) - np.pi
    turn = np.zeros_like(targets)
    turn[:, 0], turn[:, 3] = np.cos(angles / 2), np.sin(angles / 2)
    return quat_mul(turn, targets)
