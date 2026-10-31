"""Cropping the raw frame: a black strip on one edge (left by some 60 fps stream configurations), or a
custom rectangle, from the top-left unless placed (a resolution mismatch on full-sensor captures, or a
reframe). Crops keep the Bayer phase."""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

import numpy as np
import numpy.typing as npt

from mcrawsome.io.clip import Clip, RawFrame
from mcrawsome.io.metadata import FrameMetadata

_STRIP_LEVEL = 0.01  # a line of a black strip averages within 1% of the range above black
_SCENE_LEVEL = 0.05  # frames whose median is darker than this are too dark to tell a strip from the scene
_STRIP_SHARE = 0.25  # a strip covers less than this share of the frame; more is the scene


class CropError(ValueError):
    """The Recipe's crop does not fit this clip."""


@dataclass(frozen=True)
class CropBox:
    left: int
    top: int
    width: int
    height: int

    def __str__(self) -> str:
        return f"{self.width}x{self.height}+{self.left}+{self.top}"


def parse_crop(text: str) -> Literal["none", "auto"] | CropBox:
    """`none`, `auto`, `WxH` from the top-left, or `WxH+L+T` placed L pixels from the left and T from the
    top of the recorded frame (even numbers, as `CropBox` prints itself)."""
    if text in ("none", "auto"):
        return text  # type: ignore[return-value]
    found = re.fullmatch(r"(\d+)x(\d+)(?:\+(\d+)\+(\d+))?", text)
    if not found:
        raise ValueError(
            "crop must be none, auto, a size such as 3840x2160 or a placed size such as 3840x2160+64+32"
        )
    width, height = int(found[1]), int(found[2])
    left, top = int(found[3] or 0), int(found[4] or 0)
    if width < 2 or height < 2 or width % 2 or height % 2:
        raise ValueError("a crop's width and height must be even numbers (the Bayer pattern is 2x2)")
    if left % 2 or top % 2:
        raise ValueError("a crop's position must be even numbers (the Bayer pattern is 2x2)")
    return CropBox(left, top, width, height)


def _strip(means: npt.NDArray[np.float64], limit: float) -> int:
    """How many lines from the start of `means` are black."""
    dark = means <= limit
    count = int(np.argmin(dark)) if not dark.all() else len(dark)
    return count if count < _STRIP_SHARE * len(means) else 0


def find_black_edges(frames: Sequence[npt.NDArray[np.uint16]], black: float, white: float) -> CropBox | None:
    """The box inside the black strips found on the same edges of every frame (too dark frames are not
    judged), or None when there are none."""
    span = white - black
    edges: list[tuple[int, int, int, int]] = []
    for frame in frames:
        if float(np.median(frame)) - black < _SCENE_LEVEL * span:
            continue
        rows, cols = frame.mean(axis=1), frame.mean(axis=0)
        limit = black + _STRIP_LEVEL * span
        edges.append(
            (_strip(cols, limit), _strip(rows, limit), _strip(cols[::-1], limit), _strip(rows[::-1], limit))
        )
    if not edges:
        return None
    left, top, right, bottom = (min(edge[i] for edge in edges) for i in range(4))
    if not any((left, top, right, bottom)):
        return None
    height, width = frames[0].shape
    left, top = left + left % 2, top + top % 2
    return CropBox(left, top, (width - left - right) // 2 * 2, (height - top - bottom) // 2 * 2)


def crop_metadata(frame: FrameMetadata, box: CropBox) -> FrameMetadata:
    """The frame's metadata for the cropped area: its size, and where it lies on the sensor (so the lens
    shading map, which covers the whole sensor, stays aligned)."""
    left = frame.sensor_left if frame.sensor_left is not None else (frame.original_width - frame.width) / 2
    top = frame.sensor_top if frame.sensor_top is not None else (frame.original_height - frame.height) / 2
    return frame.model_copy(
        update={
            "width": box.width,
            "height": box.height,
            "sensor_left": left + box.left,
            "sensor_top": top + box.top,
        }
    )


def crop_data(data: npt.NDArray[np.uint16], box: CropBox) -> npt.NDArray[np.uint16]:
    return np.ascontiguousarray(data[box.top : box.top + box.height, box.left : box.left + box.width])


def crop_raw(raw: RawFrame, box: CropBox) -> RawFrame:
    return dataclasses.replace(raw, data=crop_data(raw.data, box), metadata=crop_metadata(raw.metadata, box))


def clip_crop(clip: Clip, setting: str, black: float, white: float) -> CropBox | None:
    """The crop the Recipe's `develop.crop` gives for this clip; `auto` checks the first, middle and last
    frames. CropError when a custom size is larger than the frame."""
    choice = parse_crop(setting)
    if choice == "none":
        return None
    first = clip.frame_metadata(0)
    if choice == "auto":
        picks = sorted({0, clip.frame_count // 2, clip.frame_count - 1})
        return find_black_edges([clip.raw_frame(i).data for i in picks], black, white)
    if choice.left + choice.width > first.width or choice.top + choice.height > first.height:
        raise CropError(f"develop.crop {setting} does not fit the frame ({first.width}x{first.height})")
    return choice
