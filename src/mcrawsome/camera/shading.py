"""Lens shading gain planes from MotionCam's per-frame lens shading map."""

from __future__ import annotations

from typing import Literal

import cv2
import numpy as np
import numpy.typing as npt

from mcrawsome.io.metadata import FrameMetadata

Plane = npt.NDArray[np.float32]
ShadingMode = Literal["full", "colour", "vignette"]


def lens_map(frame: FrameMetadata, mode: ShadingMode = "full") -> npt.NDArray[np.float32]:
    """The frame's shading map as (4, height, width) gains for R, G on R rows, G on B rows and B.

    `colour` keeps only the colour shading: each point's four gains are divided by the largest of them,
    so no gain is above 1 (no channel is pushed over white) and the falloff they share, the vignette, stays.
    `vignette` corrects only the falloff: every channel takes the two greens' mean gain, so colours stay as
    recorded.
    """
    lw, lh = frame.lens_shading_map_width, frame.lens_shading_map_height
    lsm = np.array(frame.lens_shading_map, dtype=np.float32).reshape(4, lh, lw)
    if mode == "colour":
        lsm = lsm / lsm.max(axis=0, keepdims=True)
    elif mode == "vignette":
        lsm = np.repeat(((lsm[1] + lsm[2]) / 2)[None], 4, axis=0)
    return lsm


def sensor_offset(frame: FrameMetadata) -> tuple[float, float]:
    """Where the frame's top-left corner lies on the sensor: MotionCam records a centred crop."""
    left = frame.sensor_left if frame.sensor_left is not None else (frame.original_width - frame.width) / 2
    top = frame.sensor_top if frame.sensor_top is not None else (frame.original_height - frame.height) / 2
    return left, top


def shading_gains(frame: FrameMetadata, mode: ShadingMode = "full") -> tuple[Plane, Plane, Plane, Plane]:
    """Gains for the four Bayer sites (R, G on R rows, G on B rows, B) at half resolution.

    The map covers the full sensor (original size); the frame lies at `sensor_offset` on it.
    """
    lw, lh = frame.lens_shading_map_width, frame.lens_shading_map_height
    lsm = lens_map(frame, mode)
    ox, oy = sensor_offset(frame)
    xs = (ox + np.arange(frame.width // 2) * 2 + 0.5) / frame.original_width * (lw - 1)
    ys = (oy + np.arange(frame.height // 2) * 2 + 0.5) / frame.original_height * (lh - 1)
    mx, my = np.meshgrid(xs.astype(np.float32), ys.astype(np.float32))
    planes = [
        cv2.remap(lsm[c], mx, my, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE).astype(np.float32)
        for c in range(4)
    ]
    return planes[0], planes[1], planes[2], planes[3]
