"""Temperature and tint controls using the existing DNG SDK white-balance maths."""

from __future__ import annotations

from dataclasses import replace
from fractions import Fraction
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

from mcrawsome.camera.colour import with_colour_matrix
from mcrawsome.camera.dng_colour import ILLUMINANT_TEMPERATURES
from mcrawsome.colour.robertson import cct_to_uv
from mcrawsome.colour.spaces import xy_to_xyz
from mcrawsome.output.dng import DngInfo, dng_info
from mcrawsome.resolve.bin_white_balance import _endpoint, _round, _written, white_balance

if TYPE_CHECKING:
    from mcrawsome.io.clip import Clip
    from mcrawsome.io.metadata import ContainerMetadata, FrameMetadata
    from mcrawsome.recipe.schema import DevelopParams, Recipe

Neutral = tuple[float, float, float]


def validate_temperature(kelvin: float, tint: float) -> None:
    if not np.isfinite(kelvin) or not 2000 <= kelvin <= 12000:
        raise ValueError("Temperature must be between 2000 and 12000 K")
    if not np.isfinite(tint) or not -100 <= tint <= 100:
        raise ValueError("Tint must be between -100 and 100")


def neutral_for_temperature(info: DngInfo, kelvin: float, tint: float) -> Neutral:
    """Invert the native DNG neutral-to-white transform, including tag normalisation."""
    validate_temperature(kelvin, tint)
    u, v = cct_to_uv(kelvin, tint / 3000)
    denominator = 2 * u - 8 * v + 4
    xy = float(3 * u / denominator), float(2 * v / denominator)
    try:
        a, b = ILLUMINANT_TEMPERATURES[info.illuminant_1], ILLUMINANT_TEMPERATURES[info.illuminant_2]
        balance = _written(info.analog_balance or (1.0, 1.0, 1.0), 3, positive=True)
        analog = np.diag(_round(balance / np.min(balance), 1000000))
        first = _endpoint(info.colour_matrix_1, info.calibration_1, analog)
        second = _endpoint(info.colour_matrix_2, info.calibration_2, analog)
        w = float(np.clip((1 / kelvin - 1 / b) / (1 / a - 1 / b), 0, 1)) if a != b else 1.0
        neutral = (w * first + (1 - w) * second) @ xy_to_xyz(xy)
        if not np.all(np.isfinite(neutral)) or np.min(neutral) <= 0:
            raise ValueError("non-positive camera response")
        neutral /= np.max(neutral)
        result = float(neutral[0]), float(neutral[1]), float(neutral[2])
        recovered = white_balance(replace(info, as_shot_neutral=result))
        if abs(recovered[0] - kelvin) > 1 or abs(recovered[1] - tint) > 0.1:
            raise ValueError("white balance does not round trip")
        return result
    except (KeyError, ValueError, np.linalg.LinAlgError) as exc:
        raise ValueError(
            f"{kelvin:g} K with tint {tint:+g} is outside what this camera's colour matrices can "
            "represent; choose a different temperature or tint"
        ) from exc


def colour_info(container: ContainerMetadata, frame: FrameMetadata) -> DngInfo:
    """Colour tags without generating or reading lens shading opcodes."""
    return dng_info(container, frame, Fraction(24), 0, 0, baked_bits=0)


def selected_neutral(container: ContainerMetadata, frame: FrameMetadata, develop: DevelopParams) -> Neutral:
    """The custom neutral, or the supplied as-shot value (a clip's median or first)."""
    if develop.white_balance != "custom":
        return frame.as_shot_neutral
    assert develop.temperature is not None
    assert develop.tint is not None
    return neutral_for_temperature(colour_info(container, frame), develop.temperature, develop.tint)


def as_shot(clip: Clip) -> tuple[float, float]:
    from mcrawsome.pipeline.render import choose_neutral

    info = colour_info(clip.metadata, clip.frame_metadata(0))
    return white_balance(replace(info, as_shot_neutral=choose_neutral(clip, "median")))


def as_shot_values(clip: Clip) -> dict[str, float | None]:
    """Unavailable camera colour tags leave the recorded metadata readable."""
    try:
        kelvin, tint = as_shot(clip)
    except ValueError:
        return {"temperature": None, "tint": None}
    return {"temperature": kelvin, "tint": tint}


def clip_neutral(clip: Clip, develop: DevelopParams, matrix: tuple[float, ...] | None = None) -> Neutral:
    from mcrawsome.pipeline.render import choose_neutral

    if develop.white_balance != "custom":
        return choose_neutral(clip, develop.white_balance)
    container = with_colour_matrix(clip.metadata, matrix) if matrix is not None else clip.metadata
    return selected_neutral(container, clip.frame_metadata(0), develop)


def validate_clip_white_balance(path: Path, recipe: Recipe) -> None:
    """Camera-dependent limits belong to resolving a Recipe for an existing clip, not its schema."""
    if recipe.develop.white_balance != "custom" or not path.exists():
        return
    from mcrawsome.io.clip import Clip
    from mcrawsome.io.directlog import is_video_input
    from mcrawsome.recipe.load import RecipeError

    if is_video_input(path):
        raise RecipeError("Custom white balance needs a RAW source", field="develop.white_balance")
    try:
        with Clip(path) as clip:
            clip_neutral(clip, recipe.develop, recipe.camera.colour_matrix)
    except ValueError as exc:
        raise RecipeError(str(exc), field="develop.temperature") from None
