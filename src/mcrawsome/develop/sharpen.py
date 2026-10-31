"""Noise-aware video capture sharpening on scene-linear AP1, in sensor coordinates.

The unsharp detail and a coherent edge mask are softly gated by recorded luminance variance. A local
extrema clamp limits halos. One additive luminance delta preserves channel differences, including
negative and HDR values. Fixed radius and amount are captured in the clip Recipe, not fitted per frame.
"""

from __future__ import annotations

import cv2
import numpy as np

from mcrawsome.develop.noise import LUMA, Image, NoiseModel, Plane

_GRADIENT = np.array([-0.5, 0.0, 0.5], np.float32)
_NEIGHBOURHOOD = np.ones((3, 3), np.uint8)


def _blur(plane: Plane, radius: float) -> Plane:
    return np.asarray(cv2.GaussianBlur(plane, (0, 0), radius, borderType=cv2.BORDER_REFLECT), np.float32)


def capture_sharpen(
    linear: Image,
    model: NoiseModel | None,
    amount: float,
    radius: float,
    *,
    effective_frames: float | Plane = 1.0,
) -> Image:
    """Restore coherent detail; missing or invalid recorded noise leaves the frame unchanged.

    The noise model is the pre-spatial-NR model divided by accepted temporal frame counts. Keeping
    that conservative bound avoids guessing a spatial filter's residual covariance from the picture.
    """
    if amount <= 0 or model is None:
        return linear
    a, b = np.asarray(model.scale, np.float32), np.asarray(model.offset, np.float32)
    if not np.isfinite(a).all() or not np.isfinite(b).all() or (a < 0).any() or (b < 0).any():
        return linear
    if not (a > 0).any() and not (b > 0).any():
        return linear
    y = np.asarray(linear @ LUMA, np.float32)
    smooth = _blur(y, radius)
    detail = y - smooth
    dx = cv2.filter2D(smooth, -1, _GRADIENT[None, :], borderType=cv2.BORDER_REFLECT)
    dy = cv2.filter2D(smooth, -1, _GRADIENT[:, None], borderType=cv2.BORDER_REFLECT)
    energy = _blur(np.asarray(dx * dx + dy * dy, np.float32), radius)
    count = np.maximum(np.asarray(effective_frames, np.float32), 1)
    variance = np.maximum(a * np.maximum(y, 0) + b, 0) / count
    floor = np.float32(1e-20)
    edge = energy / (energy + 4 * variance + floor)
    above_noise = detail**2 / (detail**2 + 9 * variance + floor)
    sharpened = y + np.float32(amount) * edge * above_noise * detail
    sharpened = np.clip(
        sharpened,
        cv2.erode(y, _NEIGHBOURHOOD, borderType=cv2.BORDER_REFLECT),
        cv2.dilate(y, _NEIGHBOURHOOD, borderType=cv2.BORDER_REFLECT),
    )
    return np.asarray(linear + (sharpened - y)[..., None], np.float32)
