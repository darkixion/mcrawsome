"""The command line and interface's bounded, sampled analysis."""

from __future__ import annotations

import dataclasses
import tempfile
from pathlib import Path

from mcrawsome.analysis.measure import white_balance_spread
from mcrawsome.analysis.report import Report, build_report
from mcrawsome.analysis.resolve import resolve_auto
from mcrawsome.io.directlog import is_video_input, probe_direct_log
from mcrawsome.output.export import clip_job, stops
from mcrawsome.output.ffmpeg import find_ffmpeg
from mcrawsome.pipeline.measure import measure_sdr
from mcrawsome.pipeline.stabilise import plan_raw_clip, plan_video
from mcrawsome.recipe.schema import Recipe


def analyse_report(clip: Path, recipe: Recipe, workers: int, staging: Path) -> Report:
    """Measure sampled single frames and motion, without retaining the decoded clip."""
    job, frames, _ = clip_job(clip, recipe)
    resolution = resolve_auto(recipe, job, frames, workers, always_measure=True)
    recipe = resolution.recipe
    job = dataclasses.replace(job, exposure_stops=stops(recipe))
    staging.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="analyse-", dir=staging) as folder:
        if is_video_input(clip):
            ffmpeg, ffprobe = find_ffmpeg()
            motion, _ = plan_video(ffmpeg, probe_direct_log(ffprobe, clip), recipe, workers, Path(folder))
        else:
            motion = plan_raw_clip(clip, recipe, workers, Path(folder))
    light = measure_sdr(job, frames, workers)
    return build_report(resolution, white_balance_spread(clip), light, motion, not is_video_input(clip))
