"""Measure a neutral patch in displayed RAW coordinates, before the creative grade."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import cv2
import numpy as np

from mcrawsome.camera.colour import with_colour_matrix
from mcrawsome.colour.white_balance import colour_info, neutral_for_temperature, validate_temperature
from mcrawsome.develop.crop import CropBox, crop_data
from mcrawsome.develop.raw import Developer
from mcrawsome.io.clip import Clip
from mcrawsome.io.directlog import is_video_input
from mcrawsome.output.export import frame_job, read_clip_facts
from mcrawsome.pipeline.render import first_metadata
from mcrawsome.recipe.schema import Recipe
from mcrawsome.resolve.bin_white_balance import white_balance


def pick_white_balance(
    path: Path, recipe: Recipe, index: int, rectangle: tuple[int, int, int, int]
) -> tuple[float, float]:
    """Return Kelvin and tint for (left, top, width, height) after RAW crop and display turns.

    `index` counts output frames, like the preview, including repeated or rate-mapped frames.
    At least half of the patch must survive the black/clipping mask and demosaic margin.
    """
    if is_video_input(path):
        raise ValueError("The neutral picker needs a RAW clip")
    facts = read_clip_facts(path, recipe, audio=False)
    if not 0 <= index < facts.frame_count:
        raise ValueError(f"Picker frame must be from 0 to {facts.frame_count - 1}")
    job = frame_job(path, recipe, facts)
    with Clip(path) as clip:
        container = clip.metadata
        if job.colour_matrix is not None:
            container = with_colour_matrix(container, job.colour_matrix)
        metadata = first_metadata(job, clip)
        developer = Developer(
            container,
            metadata,
            facts.neutral,
            0,
            job.lens_shading,
            shading_mode=job.lens_shading_mode,
            black_level=job.black_level,
            white_level=job.white_level,
        )
        source = facts.sources[index] if facts.sources is not None else index
        raw = clip.raw_frame(source).data
        if job.crop is not None:
            raw = crop_data(raw, CropBox(*job.crop))
        # Reject sensor clipping even if highlight reconstruction could hide it in the preview.
        invalid = ((raw >= developer.white - 2) | (raw <= developer.black + 2)).astype(np.uint8)
        invalid = cv2.dilate(invalid, np.ones((5, 5), np.uint8)).astype(bool)
        rgb = developer.camera_rgb(raw, highlights=False)
        turns = -(job.rotation // 90) + (2 if developer.rotate_180 else 0)
        rgb, invalid = np.rot90(rgb, turns), np.rot90(invalid, turns)
        left, top, width, height = rectangle
        if (
            left < 0
            or top < 0
            or width < 1
            or height < 1
            or left + width > rgb.shape[1]
            or top + height > rgb.shape[0]
        ):
            raise ValueError("Picker rectangle must lie within the output frame and have a positive size")
        patch = rgb[top : top + height, left : left + width]
        valid = ~invalid[top : top + height, left : left + width]
        valid &= np.all(np.isfinite(patch), axis=2) & np.all(patch > 0, axis=2)
        if np.count_nonzero(valid) * 2 <= width * height:
            raise ValueError(
                "The picker rectangle is mostly clipped or black; choose a midtone neutral patch"
            )
        # Undo the current balance after averaging its camera RGB. The new balance divides out these means.
        neutral = np.mean(patch[valid], axis=0, dtype=np.float64) * np.asarray(facts.neutral)
        neutral /= np.max(neutral)
        info = replace(
            colour_info(container, metadata),
            as_shot_neutral=(float(neutral[0]), float(neutral[1]), float(neutral[2])),
        )
        kelvin, tint = white_balance(info)
        validate_temperature(kelvin, tint)
        neutral_for_temperature(info, kelvin, tint)
        return kelvin, tint
