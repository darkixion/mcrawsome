"""Rotations and the warps they cause. Port of the prototype's stab.py.

Camera frame: x right, y down, z forward. A camera rotation R maps pixels as p' ~ K R K^-1 p.
Quaternions are (w, x, y, z) along the last axis.
"""

from __future__ import annotations

import numpy as np
import numpy.typing as npt

Array = npt.NDArray[np.float64]


def quat_mul(a: Array, b: Array) -> Array:
    w1, x1, y1, z1 = np.moveaxis(a, -1, 0)
    w2, x2, y2, z2 = np.moveaxis(b, -1, 0)
    return np.stack(
        [
            w1 * w2 - x1 * x2 - y1 * y2 - z1 * z2,
            w1 * x2 + x1 * w2 + y1 * z2 - z1 * y2,
            w1 * y2 - x1 * z2 + y1 * w2 + z1 * x2,
            w1 * z2 + x1 * y2 - y1 * x2 + z1 * w2,
        ],
        -1,
    )


def quat_conjugate(q: Array) -> Array:
    return np.asarray(q * np.array([1.0, -1.0, -1.0, -1.0]), dtype=np.float64)


def quat_from_rotvec(r: Array) -> Array:
    theta = np.linalg.norm(r, axis=-1, keepdims=True)
    k = np.where(theta > 1e-12, np.sin(theta / 2) / np.maximum(theta, 1e-12), 0.5)
    return np.concatenate([np.cos(theta / 2), r * k], -1)


def quat_to_matrix(q: Array) -> Array:
    q = q / np.linalg.norm(q, axis=-1, keepdims=True)
    w, x, y, z = np.moveaxis(q, -1, 0)
    return np.stack(
        [
            np.stack([1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)], -1),
            np.stack([2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)], -1),
            np.stack([2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)], -1),
        ],
        -2,
    )


def align_signs(q: Array) -> Array:
    """Flip quaternions so neighbours are on the same hemisphere (q and -q are the same rotation)."""
    q = q.copy()
    for i in range(1, len(q)):
        if (q[i] * q[i - 1]).sum() < 0:
            q[i] = -q[i]
    return q


def smooth_quats(q: Array, sigma_frames: float) -> Array:
    """Gaussian smoothing of a quaternion track (sign-aligned weighted average, renormalised)."""
    q = align_signs(q)
    radius = int(3 * sigma_frames) + 1
    kernel = np.exp(-0.5 * (np.arange(-radius, radius + 1) / sigma_frames) ** 2)
    out = np.empty_like(q)
    for i in range(len(q)):
        lo, hi = max(0, i - radius), min(len(q), i + radius + 1)
        weights = kernel[lo - i + radius : hi - i + radius]
        s = (q[lo:hi] * weights[:, None]).sum(0)
        out[i] = s / np.linalg.norm(s)
    return out


def intrinsics(focal: float, width: int, height: int) -> Array:
    return np.array([[focal, 0.0, width / 2], [0.0, focal, height / 2], [0.0, 0.0, 1.0]])


def zoom_matrix(zoom: float, width: int, height: int) -> Array:
    return np.array(
        [[zoom, 0.0, width / 2 * (1 - zoom)], [0.0, zoom, height / 2 * (1 - zoom)], [0.0, 0.0, 1.0]]
    )


def homography(rotation: Array, focal: float, width: int, height: int, zoom: float = 1.0) -> Array:
    """Source pixel to output pixel for a camera rotation, with the output zoomed about the centre."""
    k = intrinsics(focal, width, height)
    return np.asarray(zoom_matrix(zoom, width, height) @ k @ rotation @ np.linalg.inv(k), dtype=np.float64)


def warp_maps(
    bands: Array, focal: float, zoom: float, width: int, height: int
) -> tuple[npt.NDArray[np.float32], npt.NDArray[np.float32]]:
    """cv2.remap maps for one frame (output pixel to source pixel), one rotation per row band
    (rolling shutter), interpolated row by row."""
    k = intrinsics(focal, width, height)
    inverse = np.linalg.inv(zoom_matrix(zoom, width, height) @ k @ bands @ np.linalg.inv(k))
    count = len(bands)
    band_rows = (np.arange(count) + 0.5) / count * height
    rows = np.arange(height, dtype=np.float64)
    per_row = np.stack(
        [np.stack([np.interp(rows, band_rows, inverse[:, i, j]) for j in range(3)], -1) for i in range(3)], -2
    )
    xs = np.arange(width, dtype=np.float64)
    mx, my = np.empty((height, width), np.float32), np.empty((height, width), np.float32)
    # Keep double-precision projection arithmetic, but limit its temporary arrays to row strips.
    # Only the final OpenCV maps need to occupy a whole frame.
    for start in range(0, height, 128):
        end = min(start + 128, height)
        r, y = per_row[start:end], rows[start:end]
        den = r[:, 2, 0, None] * xs + (r[:, 2, 1] * y + r[:, 2, 2])[:, None]
        mx[start:end] = (r[:, 0, 0, None] * xs + (r[:, 0, 1] * y + r[:, 0, 2])[:, None]) / den
        my[start:end] = (r[:, 1, 0, None] * xs + (r[:, 1, 1] * y + r[:, 1, 2])[:, None]) / den
    return mx, my
