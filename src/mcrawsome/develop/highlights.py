"""Highlight recovery: rebuild clipped colour channels from the unclipped ones ("inpaint opposed").

Where one or two channels of a pixel reached sensor white, their true value is unknown but the surviving
channels still carry the brightness. Each clipped channel is estimated as the mean of the other two
times a colour ratio measured on unclipped pixels just outside the clipped areas, as darktable's and
RawTherapee's inpaint-opposed methods do. Pixels clipped in every channel carry no colour at all and are
left to the caller.
"""

from __future__ import annotations

import cv2
import numpy as np
import numpy.typing as npt

Image = npt.NDArray[np.float32]
_RING_PX = 8  # how far outside a clipped area the colour ratio is measured
_EPS = 1e-6


def recover_highlights(rgb: Image, clipped: npt.NDArray[np.bool_], strength: float) -> Image:
    """`rgb` is white-balanced camera RGB (h, w, 3); `clipped` (h, w, 3) marks channels at sensor white.
    Only pixels with at least one clipped and one unclipped channel change; a channel is never lowered."""
    if strength <= 0 or not clipped.any():
        return rgb
    # Per-channel operations: much faster than any(-1) and all(-1) over a last axis of three.
    each = [clipped[..., c] for c in range(3)]
    any_clipped = each[0] | each[1] | each[2]
    partial = any_clipped & ~(each[0] & each[1] & each[2])
    if not partial.any():
        return rgb
    size = 2 * _RING_PX + 1
    ring = cv2.dilate(any_clipped.astype(np.uint8), np.ones((size, size), np.uint8)).astype(bool)
    ring &= ~any_clipped
    channels = list(cv2.split(rgb))  # contiguous planes: much faster than strided channel views
    out = list(channels)
    for c in range(3):
        target = each[c] & partial
        if not target.any():
            continue
        a, b = (channels[k] for k in range(3) if k != c)
        opposed = (a + b) * np.float32(0.5)
        reference = ring & (opposed > _EPS)
        ratio = float(np.median(channels[c][reference] / opposed[reference])) if reference.any() else 1.0
        current = channels[c]
        rebuilt = np.maximum(current, opposed * np.float32(ratio))
        out[c] = np.where(target, current + np.float32(strength) * (rebuilt - current), current)
    return np.asarray(cv2.merge(out), dtype=np.float32)
