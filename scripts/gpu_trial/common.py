"""Shared contracts for the isolated development trial. No product imports this package."""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
import numpy.typing as npt

from mcrawsome.output.dng import half_cfa

Raw = npt.NDArray[np.uint16]
Float = npt.NDArray[np.float32]


def approved_clip(path: Path) -> Path:
    name = "261004_174122_VIDEO_26mm.mcraw"
    if path.name != name or path.resolve().name != name:
        raise ValueError("only the explicitly approved clip is allowed")
    return path.resolve(strict=True)


def reduce_bayer(raw: Raw) -> Raw:
    """Halve both dimensions by averaging same-colour sites, preserving the Bayer phase."""
    h, w = raw.shape
    if h % 4 or w % 4:
        raise ValueError("Bayer dimensions must be a multiple of four")
    return half_cfa(raw, "rggb")  # the helper keeps all four sites separate for any CFA


def reduce_plane(plane: Float) -> Float:
    """Average a calibration plane at the same sites as reduce_bayer."""
    small = np.empty((plane.shape[0] // 2, plane.shape[1] // 2), np.float32)
    for y in range(2):
        for x in range(2):
            small[y::2, x::2] = cv2.resize(
                plane[y::2, x::2], (plane.shape[1] // 4, plane.shape[0] // 4), interpolation=cv2.INTER_AREA
            )
    return small


def frame_order(count: int, mode: str, frames: int) -> list[int]:
    if count < 2 or frames < 1 or mode not in ("scrub", "playback"):
        raise ValueError("need at least two frames and playback or scrub")
    cycle = list(range(count))
    if mode == "scrub":
        cycle += list(range(count - 2, 0, -1))
    return [cycle[i % len(cycle)] for i in range(frames)]


def difference(image: npt.NDArray[np.uint8], reference: npt.NDArray[np.uint8]) -> dict[str, float]:
    error = np.abs(image.astype(np.float32) - reference.astype(np.float32))
    return {"mean_8bit": float(error.mean()), "p99_8bit": float(np.percentile(error, 99))}
