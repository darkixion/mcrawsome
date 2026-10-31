"""The stabilisation plan: one correction rotation per frame and row band. Port of stab_plan.py.

The camera's real orientation is smoothed over time; each frame is turned from where the camera really
pointed (row by row, for the rolling shutter) to where the smoothed camera points. Where a correction
would show the frame's edge through the crop, the smoothing is pulled back towards the real path for that
stretch, gradually, so the strength never jumps.

Modes choose where the camera should point instead: `smooth` its smoothed path; `rolling_shutter` its real
path, so only the rows are straightened and the framing stays as shot; `tripod` one fixed view; `pan` the
smoothed path's pan alone, tilt and roll held. Tripod and pan hold the clip's mean orientation.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import numpy as np
import numpy.typing as npt

from mcrawsome.motion.geometry import (
    align_signs,
    intrinsics,
    quat_conjugate,
    quat_mul,
    quat_to_matrix,
    smooth_quats,
    zoom_matrix,
)
from mcrawsome.motion.orientation import Orientation

Array = npt.NDArray[np.float64]
Mode = Literal["smooth", "rolling_shutter", "tripod", "pan"]


@dataclass(frozen=True)
class StabilisationPlan:
    rotations: npt.NDArray[np.float32]  # (frames, bands, 3, 3): real (per row band) to stabilised
    target_quats: Array  # (frames, 4): the stabilised orientation, for reports and tests
    focal_px: float
    zoom: float
    width: int
    height: int
    reduced_frames: int
    mean_correction_deg: float
    max_correction_deg: float
    coverage: float
    rotate_180: bool = False
    reduced_rolling_shutter_frames: int = 0  # frames whose rolling-shutter correction was eased
    followed_frames: int = 0  # frames that had to follow the real camera partly, fast shake included

    def save(self, path: Path) -> None:
        np.savez(
            path,
            rotations=self.rotations,
            target_quats=self.target_quats,
            numbers=np.array([self.focal_px, self.zoom, self.width, self.height, self.reduced_frames,
                              self.mean_correction_deg, self.max_correction_deg, self.coverage,
                              float(self.rotate_180), self.reduced_rolling_shutter_frames,
                              self.followed_frames]),
        )  # fmt: skip

    @classmethod
    def load(cls, path: Path) -> StabilisationPlan:
        with np.load(path) as data:
            n = data["numbers"]
            return cls(
                data["rotations"], data["target_quats"], float(n[0]), float(n[1]), int(n[2]), int(n[3]),
                int(n[4]), float(n[5]), float(n[6]), float(n[7]), bool(n[8]), int(n[9]), int(n[10]),
            )  # fmt: skip


def _border(width: int, height: int) -> Array:
    xs, ys = np.linspace(0, width, 33), np.linspace(0, height, 19)
    return np.concatenate(
        [
            np.stack([xs, np.zeros(33)], 1),
            np.stack([xs, np.full(33, height)], 1),
            np.stack([np.zeros(19), ys], 1),
            np.stack([np.full(19, width), ys], 1),
        ]
    )


def border_inside(
    band_rotations: npt.ArrayLike,
    focal: float,
    zoom: float,
    width: int,
    height: int,
    *,
    edge_rows: bool = False,
) -> bool:
    """Whether every point of the output frame's edge maps back inside the source frame."""
    rotations = np.asarray(band_rotations, dtype=np.float64)
    return _border_test(focal, zoom, width, height, len(rotations), edge_rows)(rotations)


def _border_test(
    focal: float,
    zoom: float,
    width: int,
    height: int,
    bands: int,
    edge_rows: bool = False,
) -> Callable[[Array], bool]:
    """Reuse the fixed projection and border samples throughout a crop solve."""
    k = intrinsics(focal, width, height)
    basis = zoom_matrix(zoom, width, height) @ k
    inverse_k = np.linalg.inv(k)
    points = _border(width, height)
    which = np.minimum((points[:, 1] / height * bands).astype(int), bands - 1)
    homogeneous = np.column_stack([points, np.ones(len(points))])

    def inside(rotations: Array) -> bool:
        inverse = np.linalg.inv(basis @ rotations @ inverse_k)
        if edge_rows and bands > 1:
            # Resolve interpolates poses at the source sensor row, not the output
            # strip. Iterate the inverse lookup because the warp changes that row.
            source_y = points[:, 1].copy()
            for _ in range(4):
                position = np.clip(source_y / height * (bands - 1), 0, bands - 1)
                low = position.astype(int)
                high = np.minimum(low + 1, bands - 1)
                fraction = (position - low)[:, None, None]
                matrices = inverse[low] * (1 - fraction) + inverse[high] * fraction
                source = np.einsum("nij,nj->ni", matrices, homogeneous)
                source_y = source[:, 1] / source[:, 2]
        else:
            source = np.einsum("nij,nj->ni", inverse[which], homogeneous)
        sx, sy = source[:, 0] / source[:, 2], source[:, 1] / source[:, 2]
        margin = 1.0 if edge_rows and zoom > 1.001 else 0.0
        return bool(
            np.all(
                (sx >= margin - 1e-6)
                & (sx <= width - margin + 1e-6)
                & (sy >= margin - 1e-6)
                & (sy <= height - margin + 1e-6)
            )
        )

    return inside


def jitter(quats: Array, fps: float) -> float:
    """RMS of the high-frequency part of the frame-to-frame rotation, in degrees per frame."""
    steps = quat_mul(quats[1:], quat_conjugate(quats[:-1]))
    steps = steps * np.sign(steps[:, :1] + 1e-12)
    rates = 2 * steps[:, 1:]  # small-angle rotation vectors
    sigma = 0.5 * fps
    radius = int(3 * sigma)
    kernel = np.exp(-0.5 * (np.arange(-radius, radius + 1) / sigma) ** 2)
    kernel /= kernel.sum()
    padded = np.pad(rates, ((radius, radius), (0, 0)), mode="edge")
    smooth = np.stack([np.convolve(padded[:, k], kernel, "valid") for k in range(3)], -1)
    return float(np.degrees(np.sqrt(np.mean(np.sum((rates - smooth) ** 2, 1)))))


def nearest_covered(covered: npt.NDArray[np.bool_]) -> npt.NDArray[np.int64]:
    """For each frame, the index of the nearest covered frame (itself when covered)."""
    index = np.flatnonzero(covered)
    frames = np.arange(len(covered))
    right = np.clip(np.searchsorted(index, frames), 0, len(index) - 1)
    left = np.clip(right - 1, 0, len(index) - 1)
    closer_left = np.abs(frames - index[left]) <= np.abs(index[right] - frames)
    return np.asarray(np.where(closer_left, index[left], index[right]), dtype=np.int64)


def mean_orientation(quats: Array) -> Array:
    """The average of a track of orientations (sign-aligned, renormalised)."""
    total = align_signs(quats).sum(0)
    return np.asarray(total / np.linalg.norm(total), dtype=np.float64)


def pan_only(targets: Array, reference: Array, axis: int) -> Array:
    """Each target turned from `reference` about the camera's `axis` alone (the pan), the other turns held.

    Turns compose on the left in camera axes, so the turn from the reference is target x reference*; its
    twist about the axis keeps that axis's component of the quaternion."""
    turn = quat_mul(targets, quat_conjugate(reference)[None, :])
    twist = np.zeros_like(turn)
    twist[:, 0], twist[:, 1 + axis] = turn[:, 0], turn[:, 1 + axis]
    length = np.linalg.norm(twist, axis=1, keepdims=True)
    twist = np.where(length > 1e-12, twist / np.maximum(length, 1e-12), np.array([1.0, 0.0, 0.0, 0.0]))
    return np.asarray(quat_mul(twist, reference[None, :]), dtype=np.float64)


def plan_stabilisation(
    orientation: Orientation,
    frame_centre_s: Array,
    readout_s: float,
    smoothing_s: float,
    crop: float,
    strength: float,
    bands: int,
    width: int,
    height: int,
    fps: float,
    focal_px: float,
    rotate_180: bool = False,
    short_smoothing_s: float = 0.1,
    edge_rows: bool = False,
    horizon_up: Array | None = None,
    horizon_rotation: int = 0,
    mode: Mode = "smooth",
    display_turn: int = 0,
) -> StabilisationPlan:
    """Per frame, a strength s from 2 * `strength` down: 2 follows the long smoothing, 1 a short
    smoothing that still removes jitter, 0 the real camera. Each frame gets the largest s that keeps
    the crop's border inside the picture, so when big slow movements exhaust the crop, fast shake is
    still removed and only the slow drift is followed. Bands are centred in equal strips of rows, or with
    `edge_rows` sit at rows k / (bands - 1), first and last row included, as Resolve's Camera Gyro row
    slots do (measured with scripts/resolve_row_probe.py). `display_turn`, the picture's clockwise turn
    on screen, tells which camera axis is the pan for `mode` "pan": y, or x when it is shown sideways."""
    covered = orientation.covers(frame_centre_s)
    if not covered.any():
        raise ValueError("the gyro log does not cover any frame")
    q = orientation.at(frame_centre_s)
    band_y = np.linspace(0.0, 1.0, bands) if edge_rows and bands > 1 else (np.arange(bands) + 0.5) / bands
    q_rows = np.stack([orientation.at(frame_centre_s + (y - 0.5) * readout_s) for y in band_y], 1)
    smoothed = smooth_quats(q, max(smoothing_s * fps, 1e-3))
    short = smooth_quats(q, max(min(short_smoothing_s, smoothing_s) * fps, 1e-3))
    if mode == "tripod":
        smoothed = np.tile(mean_orientation(q[covered]), (len(q), 1))
    elif mode == "pan":
        smoothed = pan_only(smoothed, mean_orientation(q[covered]), 0 if display_turn % 180 == 90 else 1)
    if horizon_up is not None:
        from mcrawsome.motion.horizon import level

        rotation = horizon_rotation + (180 if rotate_180 else 0)
        smoothed, short = level(smoothed, horizon_up, rotation), level(short, horizon_up, rotation)

    # Keep only the latest correction per frame, in the arrays used for the final plan. The crop
    # and rolling-shutter passes often ask for the same correction and border check again.
    n = len(q)
    rotations = np.empty((n, bands, 3, 3), dtype=np.float64)
    targets = np.empty((n, 4), dtype=np.float64)
    row_alpha, row_beta = np.full(n, np.nan), np.full(n, np.nan)
    checked, fits_border = np.zeros(n, dtype=bool), np.empty(n, dtype=bool)

    def towards(a: Array, b: Array, t: float) -> Array:
        b = b if (a * b).sum() >= 0 else -b
        v = (1 - t) * a + t * b
        return np.asarray(v / np.linalg.norm(v), dtype=np.float64)

    def target(k: int, alpha: float) -> Array:
        if alpha >= 1.0:
            return towards(short[k], smoothed[k], alpha - 1.0)
        return towards(q[k], short[k], alpha)

    def rows(k: int, alpha: float, beta: float = 1.0) -> Array:
        """Per-band corrections; beta below 1 eases the rolling-shutter part towards the frame centre's
        orientation, so alpha 0 with beta 0 is exactly no correction."""
        correction: Array = rotations[k]
        if row_alpha[k] == alpha and row_beta[k] == beta:
            return correction
        if row_alpha[k] != alpha:
            targets[k] = target(k, alpha)
        real = q_rows[k]
        if beta < 1.0:
            mid = q[k]
            aligned = np.where((real * mid).sum(-1, keepdims=True) < 0, -real, real)
            real = beta * aligned + (1 - beta) * mid
            real = real / np.linalg.norm(real, axis=-1, keepdims=True)
        correction[:] = quat_to_matrix(quat_mul(targets[k][None, :], quat_conjugate(real)))
        row_alpha[k], row_beta[k], checked[k] = alpha, beta, False
        return correction

    inside = _border_test(focal_px, crop, width, height, bands, edge_rows)

    def fits(k: int, alpha: float, beta: float = 1.0) -> bool:
        correction = rows(k, alpha, beta)
        if not checked[k]:
            fits_border[k], checked[k] = inside(correction), True
        return bool(fits_border[k])

    top = 0.0 if mode == "rolling_shutter" else 2.0 * float(strength)  # 0: the real camera, rows only
    alpha = np.full(n, top)
    for _ in range(3):
        bad = [k for k in range(n) if not fits(k, alpha[k])]
        if not bad:
            break
        for k in bad:  # the largest strength that keeps this frame inside the crop
            lo, hi = 0.0, float(alpha[k])
            for _ in range(12):
                mid = (lo + hi) / 2
                lo, hi = (mid, hi) if fits(k, mid) else (lo, mid)
            alpha[k] = lo
        radius = max(1, int(fps * 0.4))  # spread reductions smoothly in time
        alpha = np.array([alpha[max(0, i - radius) : i + radius + 1].min() for i in range(n)])
        kernel = np.exp(-0.5 * (np.arange(-radius, radius + 1) / (radius / 2)) ** 2)
        kernel /= kernel.sum()
        alpha = np.minimum(alpha, np.convolve(np.pad(alpha, radius, mode="edge"), kernel, "valid"))

    beta = np.ones(n)
    for k in range(n):  # at no stabilisation, the rolling-shutter correction alone can exceed the crop
        if not fits(k, alpha[k]):
            lo, hi = 0.0, 1.0  # beta 0 with alpha 0 is no correction at all, which always fits
            for _ in range(12):
                mid = (lo + hi) / 2
                lo, hi = (mid, hi) if fits(k, alpha[k], mid) else (lo, mid)
            beta[k] = lo
    for k in range(n):
        rows(k, alpha[k], beta[k])
    nearest = nearest_covered(covered)
    rotations, targets = rotations[nearest], targets[nearest]  # outside the log: hold the nearest frame
    middle = rotations[:, bands // 2]
    angles = np.degrees(np.arccos(np.clip((np.trace(middle, axis1=1, axis2=2) - 1) / 2, -1, 1)))
    return StabilisationPlan(
        rotations=rotations.astype(np.float32),
        target_quats=targets,
        focal_px=focal_px,
        zoom=crop,
        width=width,
        height=height,
        reduced_frames=int((alpha < top - 1e-3).sum()),
        followed_frames=int((alpha < min(1.0, top) - 1e-3).sum()),
        mean_correction_deg=float(angles.mean()),
        max_correction_deg=float(angles.max()),
        coverage=float(covered.mean()),
        rotate_180=rotate_180,
        reduced_rolling_shutter_frames=int((beta < 1.0).sum()),
    )
