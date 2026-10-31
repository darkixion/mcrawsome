"""Where the frame a stabilised timeline keeps lies in each recorded frame, for the unstabilised preview.

The outline is planned exactly as a timeline send plans the clip (`gyro.timeline_recipe`,
`gyro.plan_timeline_clip`), so it moves as Resolve's correction will, and a send afterwards reuses the plan.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from collections.abc import Callable
from concurrent.futures import CancelledError
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np
import numpy.typing as npt
from pydantic import BaseModel

from mcrawsome.motion.geometry import intrinsics, zoom_matrix
from mcrawsome.motion.plan import StabilisationPlan
from mcrawsome.recipe.schema import Recipe
from mcrawsome.resolve.gyro import plan_key, plan_timeline_clip, timeline_recipe

KEPT = 8  # outlines kept, newest last: going back to an earlier setting needs no new plan
_kept: OrderedDict[str, tuple[npt.NDArray[np.float32], str | None]] = OrderedDict()
_lock = threading.Lock()


class KeptFrame(BaseModel):
    """For each recorded frame, the kept frame's corners as fractions of the picture as shown: x, y of
    its top left, top right, bottom right and bottom left. Empty, with `reason`, when a timeline would
    not stabilise the clip with these settings."""

    corners: list[list[float]]
    reason: str | None = None


def kept_corners(plan: StabilisationPlan, rotation: int = 0) -> npt.NDArray[np.float64]:
    """(frames, 4, 2): where each output frame of `plan` comes from in its recorded frame, as fractions of
    the picture turned upright, then clockwise by `rotation`. With rolling-shutter rows, each corner
    takes the pose of the sensor row it comes from, as Resolve does (`motion.plan.border_inside`)."""
    width, height = plan.width, plan.height
    k = intrinsics(plan.focal_px, width, height)
    rotations = plan.rotations.astype(np.float64)
    inverse = np.linalg.inv(zoom_matrix(plan.zoom, width, height) @ k @ rotations @ np.linalg.inv(k))
    corners = np.array([[0.0, 0.0, 1.0], [width, 0.0, 1.0], [width, height, 1.0], [0.0, height, 1.0]])
    if plan.rotate_180:  # planned in the sensor's orientation: the upright top left is its bottom right
        corners = corners[[2, 3, 0, 1]]
    frames, bands = rotations.shape[:2]
    if bands == 1:
        source = np.einsum("fij,cj->fci", inverse[:, 0], corners)
    else:
        each = np.arange(frames)[:, None]
        source_y = np.broadcast_to(corners[:, 1], (frames, 4)).copy()
        for _ in range(4):  # the pose moves the row it is taken at: iterate, as the planner does
            position = np.clip(source_y / height * (bands - 1), 0, bands - 1)
            low = position.astype(int)
            high = np.minimum(low + 1, bands - 1)
            fraction = (position - low)[..., None, None]
            matrices = inverse[each, low] * (1 - fraction) + inverse[each, high] * fraction
            source = np.einsum("fcij,cj->fci", matrices, corners)
            source_y = source[..., 1] / source[..., 2]
    x, y = source[..., 0] / source[..., 2], source[..., 1] / source[..., 2]
    if plan.rotate_180:
        x, y = width - x, height - y
    turn = rotation % 360
    if turn == 90:
        x, y, width, height = height - y, x, height, width
    elif turn == 180:
        x, y = width - x, height - y
    elif turn == 270:
        x, y, width, height = y, width - x, height, width
    return np.stack([x / width, y / height], -1)


def kept_frame(
    path: Path, values: Recipe, stop: threading.Event, workers: int = 2, *, quick: bool = False
) -> KeptFrame:
    """The kept frame on each of the clip's frames with these settings. Planning takes seconds, so the
    newest few outlines are kept; `stop` gives up the planning. `quick` plans without image-assisted
    refinement, which takes most of the time on a clip's first plan: a first look before the plan a send
    uses."""
    if quick:
        motion = values.motion.model_copy(update={"optical_correction": False})
        values = values.model_copy(update={"motion": motion})

    def check() -> None:
        if stop.is_set():
            raise CancelledError

    develop = values.develop
    try:
        recipe, rolling_shutter = timeline_recipe(values, path.name)
    except ValueError as exc:
        return KeptFrame(corners=[], reason=str(exc))
    if develop.mount_crop != "none":
        return KeptFrame(
            corners=[], reason="Stabilised clips need uncropped media; set Mounted DNG crop to None"
        )
    key = plan_key(path, recipe, rolling_shutter, develop.rotation)
    with _lock:
        kept = _kept.get(key)
        if kept is not None:
            _kept.move_to_end(key)
    if kept is None:
        kept = _plan(path, recipe, rolling_shutter, develop.rotation, workers, check)
        with _lock:
            _kept[key] = kept
            while len(_kept) > KEPT:
                _kept.popitem(last=False)
    corners, reason = kept
    return KeptFrame(corners=np.round(corners.astype(np.float64), 5).tolist(), reason=reason)


def _plan(
    path: Path, recipe: Recipe, rolling_shutter: bool, rotation: int, workers: int, check: Callable[[], None]
) -> tuple[npt.NDArray[np.float32], str | None]:
    nothing = np.empty((0, 8), np.float32)
    with TemporaryDirectory(prefix="mcrawsome-kept-frame-") as folder:
        try:
            outcome = plan_timeline_clip(
                path,
                recipe,
                rolling_shutter,
                Path(folder),
                workers=workers,
                display_rotation=rotation,
                check=check,
            )
        except ValueError as exc:
            return nothing, str(exc)
        if outcome.plan_path is None:
            return nothing, "; ".join(outcome.notes) or f"{path.name} could not be planned"
        corners = kept_corners(StabilisationPlan.load(outcome.plan_path), rotation)
    return corners.reshape(len(corners), 8).astype(np.float32), None
