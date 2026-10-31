"""Sensor clipping before shading, white balance or remosaic; never a saved media transform."""

from __future__ import annotations

import base64
import threading
from pathlib import Path
from typing import Literal

import cv2
import numpy as np
import numpy.typing as npt
from pydantic import BaseModel

from mcrawsome.analysis.diagnostics import MAX_PIXELS, _check
from mcrawsome.camera.remosaic import detect_quad_bayer
from mcrawsome.develop.crop import crop_data
from mcrawsome.io.clip import Clip
from mcrawsome.output.export import read_clip_facts
from mcrawsome.recipe.schema import Recipe

Mode = Literal["any", "all"]


class ClippingImage(BaseModel):
    frame_index: int
    source_index: int
    width: int
    height: int
    white_level: float
    quad_bayer: bool
    url: str


def clipping_cells(
    data: npt.NDArray[np.uint16], pattern: str, white: float, mode: Mode, *, quad: bool = False
) -> npt.NDArray[np.uint8]:
    """One classification per physical CFA cell, conservatively expanded over its photosites.

    Each colour clips when any of its recorded sites reaches white; either green site suffices.
    This uses the original 4x4 layout for Quad Bayer and the 2x2 layout for ordinary Bayer.
    """
    block = 4 if quad else 2
    if data.shape[0] % block or data.shape[1] % block:
        raise ValueError("sensor clipping needs complete CFA cells")
    channels = {colour: np.zeros((data.shape[0] // block, data.shape[1] // block), bool) for colour in "rgb"}
    for y in range(block):
        for x in range(block):
            site = 2 * (y // 2) + x // 2 if quad else 2 * y + x
            channels[pattern[site]] |= data[y::block, x::block] >= white
    mask = (
        channels["r"] | channels["g"] | channels["b"]
        if mode == "any"
        else (channels["r"] & channels["g"] & channels["b"])
    )
    return np.repeat(np.repeat(mask.astype(np.uint8), block, axis=0), block, axis=1)


def measure_clipping(
    path: Path, recipe: Recipe, index: int, mode: Mode, width: int, stop: threading.Event
) -> ClippingImage:
    _check(stop)
    facts = read_clip_facts(path, recipe, audio=False)
    if not 0 <= index < facts.frame_count:
        raise ValueError("clipping frame is outside the recording")
    source = facts.sources[index] if facts.sources is not None else index
    with Clip(path, quad_bayer_remosaic=False) as clip:
        first = clip.frame_metadata(0)
        meta = clip.frame_metadata(source)
        if min(meta.width, meta.height) < 2 or meta.width * meta.height > MAX_PIXELS:
            raise ValueError("sensor clipping needs at most 64 Mi photosites")
        raw = clip.raw_frame(source)
        _check(stop)
        quad = first.need_remosaic
        if (
            not quad
            and first.is_binned is False
            and (first.original_width >= 2 * first.width and first.original_height >= 2 * first.height)
        ):
            recorded_first = raw if source == 0 else clip.raw_frame(0)
            quad = detect_quad_bayer(recorded_first.data, clip.metadata.sensor_arrangement, first.black_level)
        mask = clipping_cells(
            raw.data, clip.metadata.sensor_arrangement, raw.metadata.white_level, mode, quad=quad
        )
    if facts.crop is not None:
        mask = crop_data(mask.astype(np.uint16), facts.crop).astype(np.uint8)
    if first.orientation == 3:
        mask = np.rot90(mask, 2)
    if recipe.develop.rotation:
        mask = np.rot90(mask, -recipe.develop.rotation // 90)
    scale = min(1.0, width / mask.shape[1])
    size = (max(1, round(mask.shape[1] * scale)), max(1, round(mask.shape[0] * scale)))
    # Area resampling followed by >0 retains even an isolated clipped cell when shrinking.
    mask = (cv2.resize(mask.astype(np.float32), size, interpolation=cv2.INTER_AREA) > 0).astype(np.uint8)
    pixels = np.zeros((*mask.shape, 4), np.uint8)
    pixels[..., :3] = (180, 0, 255)  # BGRA pink; transparent everywhere else
    pixels[..., 3] = mask * 180
    _check(stop)
    ok, encoded = cv2.imencode(".png", pixels)
    if not ok:
        raise ValueError("could not encode clipping image")
    return ClippingImage(
        frame_index=index,
        source_index=source,
        width=size[0],
        height=size[1],
        white_level=raw.metadata.white_level,
        quad_bayer=quad,
        url="data:image/png;base64," + base64.b64encode(encoded).decode(),
    )
