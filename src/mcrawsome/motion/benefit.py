"""How much of the measured shake a stabilisation plan removes, without rendering anything.

The plan moves the picture's centre by a known amount in each frame; the image motion measured between
frames (the sync fit's track) plus the change in that movement is what the viewer would still see.
The share is computed on the frame-to-frame (high-frequency) part, as `sync.shake_removed` is.
"""

from __future__ import annotations

import numpy as np
import numpy.typing as npt

from mcrawsome.motion.flow import MotionTrack
from mcrawsome.motion.geometry import homography
from mcrawsome.motion.plan import StabilisationPlan

Array = npt.NDArray[np.float64]


def _high(x: Array) -> Array:
    from scipy.ndimage import uniform_filter1d  # scipy loads only when needed

    return np.asarray(x - uniform_filter1d(x, 9, axis=0, mode="nearest"), dtype=np.float64)


def centre_shift(plan: StabilisationPlan) -> Array:
    """(frames, 2): where the plan moves the picture's centre, in source pixels."""
    middle = plan.rotations[:, plan.rotations.shape[1] // 2].astype(np.float64)
    c = np.array([plan.width / 2, plan.height / 2, 1.0])
    points = np.stack([homography(r, plan.focal_px, plan.width, plan.height, plan.zoom) @ c for r in middle])
    return np.asarray((points[:, :2] / points[:, 2:] - c[:2]) / plan.zoom, dtype=np.float64)


def shake_removed_by_plan(plan: StabilisationPlan, track: MotionTrack) -> float:
    """1 means all the measured frame-to-frame shake is cancelled, 0 none, below 0 more is added."""
    if len(track.first) < 3:
        return 0.0
    shift = centre_shift(plan)
    first = np.clip(track.first, 0, len(shift) - 2)
    measured = np.stack([track.dx, track.dy], -1)
    seen = measured + (shift[first + 1] - shift[first])
    before = float(np.sqrt((_high(measured) ** 2).sum(1).mean()))
    after = float(np.sqrt((_high(seen) ** 2).sum(1).mean()))
    return 1.0 - after / before if before > 1e-9 else 0.0
