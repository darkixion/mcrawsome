"""Resolve the Recipe's automatic values from measurements, with the reasons."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from mcrawsome.analysis.measure import SampleStats, measure_samples
from mcrawsome.pipeline.render import FrameJob
from mcrawsome.progress import Progress
from mcrawsome.recipe.schema import Recipe

TARGET_KEY = 0.165  # scene log-average luminance auto exposure aims for (fitted to the owner's choices)
MIN_STOPS, MAX_STOPS = -2.0, 3.0


def auto_exposure(stats: SampleStats) -> tuple[float, str | None]:
    """Stops that bring the clip's key to TARGET_KEY, within MIN_STOPS..MAX_STOPS, to 0.1 stop."""
    if not math.isfinite(stats.key) or stats.key <= 1e-6:
        return (
            MAX_STOPS,
            f"the frames are too dark to judge; auto exposure used its largest push ({MAX_STOPS:+.0f})",
        )
    stops = math.log2(TARGET_KEY / stats.key)
    held = min(max(stops, MIN_STOPS), MAX_STOPS)
    note = None
    if held != stops:
        note = (
            f"auto exposure wanted {stops:+.1f} stops and was held at {held:+.0f}; "
            "set --develop.exposure to choose"
        )
    return round(held, 1), note


@dataclass(frozen=True)
class Resolution:
    recipe: Recipe  # every automatic value replaced by the value used
    notes: list[str] = field(default_factory=list)
    measurements: dict[str, Any] = field(default_factory=dict)
    stats: SampleStats | None = None


def resolve_auto(
    recipe: Recipe,
    job: FrameJob,
    frame_count: int,
    workers: int,
    always_measure: bool = False,
    progress: Progress | None = None,
) -> Resolution:
    """Measure when something is automatic (or `always_measure`, for the report); a numeric exposure is
    used as it is."""
    if recipe.develop.exposure != "auto":
        if not always_measure:
            return Resolution(recipe)
        return Resolution(recipe, stats=measure_samples(job, frame_count, workers, progress))
    stats = measure_samples(job, frame_count, workers, progress)
    stops, note = auto_exposure(stats)
    develop = recipe.develop.model_copy(update={"exposure": stops})
    measurements = {
        "exposure_auto": True,
        "exposure_key": round(stats.key, 5),
        "clipped_share": round(stats.clipped_share, 5),
        "noise_mid_grey": round(stats.noise_mid_grey(stops), 5),
        "frames_sampled": stats.frames,
    }
    resolved = recipe.model_copy(update={"develop": develop})
    return Resolution(resolved, [note] if note else [], measurements, stats)
