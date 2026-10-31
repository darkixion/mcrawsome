"""The recorded sensor noise, projected to developed AP1 luminance (for capture sharpening)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

import cv2
import numpy as np
import numpy.typing as npt

from mcrawsome.colour.spaces import AP1_LUMINANCE

if TYPE_CHECKING:
    from mcrawsome.develop.raw import Developer

Image = npt.NDArray[np.float32]
Plane = npt.NDArray[np.float32]
LUMA = AP1_LUMINANCE.astype(np.float32)


@dataclass(frozen=True)
class NoiseModel:
    """Variance of developed luminance y: scale * y + offset, before temporal averaging."""

    scale: float | Plane
    offset: float | Plane


def _profile_pairs(profile: Sequence[float]) -> npt.NDArray[np.float64] | None:
    values = np.asarray(profile, np.float64)
    if values.size not in (2, 6, 8) or not np.isfinite(values).all() or (values < 0).any():
        return None
    pairs = values.reshape(-1, 2)
    if len(pairs) == 1:
        pairs = np.repeat(pairs, 3, axis=0)
    elif len(pairs) == 4:  # MotionCam: R, G even, G odd, B, independently of the CFA arrangement
        pairs = np.stack((pairs[0], pairs[1:3].mean(0), pairs[3]))
    return pairs if pairs.any() else None


def recorded_noise_model(developer: Developer, profile: Sequence[float]) -> NoiseModel | None:
    """Project a recorded sensor profile through shading, white balance and the camera matrix.

    A neutral scene relates sensor signal to AP1 luminance. Demosaic creates correlated noise, so this
    uses the variance of an individual colour sample rather than claiming a demosaic averaging gain.
    The two green sites share their contribution. Coefficients vary smoothly with the lens shading.
    """
    pairs = _profile_pairs(profile)
    if pairs is None:
        return None
    height, width = developer.gain.shape
    shape = (height // 2, width // 2)
    signal, shot, read = (np.zeros(shape, np.float32) for _ in range(3))
    weights = LUMA @ developer.matrix
    for i, (y, x) in enumerate(((0, 0), (0, 1), (1, 0), (1, 1))):
        colour = developer.cfa[i]
        channel = "rgb".index(colour)
        scale, offset = pairs[channel]
        recorded_black = developer.recorded_blacks[i]
        span = developer.recorded_white - recorded_black
        if span <= 0:
            return None
        gain = developer.gain[y : 2 * shape[0] : 2, x : 2 * shape[1] : 2] * np.float32(span)
        projected = np.float32(weights[channel]) * gain
        share = np.float32(0.5 if colour == "g" else 1)
        signal += share * projected
        shot += share * projected**2 * np.float32(scale)
        black_shift = (float(developer.black[y, x]) - recorded_black) / span
        read += share * projected**2 * np.float32(max(0, offset + scale * black_shift))
    a = cv2.resize(shot / np.maximum(signal, np.float32(1e-8)), (width, height))
    b = cv2.resize(read, (width, height))
    if developer.rotate_180:
        a, b = a[::-1, ::-1], b[::-1, ::-1]
    return NoiseModel(np.ascontiguousarray(a, np.float32), np.ascontiguousarray(b, np.float32))
