"""A clip's gyro, frame times, exposure and focus, gathered for the motion summary (no picture decoded).

RAW clips use the motion recorded in them (MotionCam 5.1 and later) on the frames' own clock, else the
`.gcsv` beside them, linked to the frames through their receive times and the camera profile's sync
offset, as the review markers do. Direct Log videos use their embedded telemetry on the
video's own clock; they record no shutter, so blur assumes a 180-degree shutter, as stabilisation does.
`every_frame=False` reads only the first frames' receive times (about a frame's accuracy), which is
enough for the movement and quick enough for a library scan.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
from pydantic import BaseModel, Field

from mcrawsome.analysis.motion_summary import (
    Array,
    MotionSummary,
    Movement,
    Rates,
    TrimSuggestion,
    blurred_stretches,
    frame_blur,
    movement,
    rates,
    steadiest_frame,
    suggest_trim,
)
from mcrawsome.io.gcsv import GcsvError
from mcrawsome.io.gyro import GyroLog

if TYPE_CHECKING:
    from mcrawsome.io.directlog import DirectLogInfo
    from mcrawsome.recipe.schema import Recipe

log = logging.getLogger(__name__)
LOG_EXPOSURE_NOTE = "Direct Log videos do not record the shutter: blur assumes a 180-degree shutter"
_LINK_FRAMES = 8  # receive times read when not every frame is wanted


@dataclass(frozen=True)
class ClipMotion:
    rates: Rates
    centres: Array  # each recorded frame's mid-exposure, on the log's clock
    exposure_s: Array
    focal_px: float
    first_start: float  # the first frame's start on the log's clock
    duration_s: float
    frame_rate: float
    focus: Array | None  # dioptres per frame, NaN where not recorded
    note: str | None = None

    def summary(self) -> MotionSummary:
        return movement(self.rates, self.first_start, self.first_start + self.duration_s)

    def trim(self) -> TrimSuggestion:
        return suggest_trim(self.rates, self.first_start, self.duration_s, self.focus, self.frame_rate)

    def blur(self) -> Array:
        return frame_blur(self.rates, self.centres, self.exposure_s, self.focal_px)

    def steadiest(self) -> int | None:
        return steadiest_frame(self.blur(), self.focus)

    def facts(self) -> dict[str, object]:
        """What the library lists: the movement, the shake blur most frames stay under (95th percentile)
        and the seconds of sustained blur."""
        blur = self.blur()
        finite = blur[np.isfinite(blur)]
        stretches = blurred_stretches(blur, self.frame_rate)
        return {
            "movement": self.summary().movement,
            "shake_blur_px": round(float(np.percentile(finite, 95)), 1) if len(finite) else None,
            "blurred_s": round(sum(count for _, count, _ in stretches) / self.frame_rate, 2),
        }


def load(
    path: Path, *, every_frame: bool = True, check: Callable[[], None] = lambda: None
) -> ClipMotion | None:
    """The clip's motion data, or None when it has no usable gyro log."""
    from mcrawsome.camera.profile import ProfileError
    from mcrawsome.io.container import ContainerError
    from mcrawsome.io.directlog import is_video_input
    from mcrawsome.motion.raw_gyro import GyroError

    try:
        return _video(path, every_frame) if is_video_input(path) else _raw(path, every_frame, check)
    except (GcsvError, GyroError, ValueError, ProfileError, ContainerError, OSError, IndexError) as exc:
        log.info("No motion summary for %s: %s", path.name, exc)
        return None


def _recipe(path: Path) -> Recipe:
    from mcrawsome.recipe.clip_layer import read_layer
    from mcrawsome.recipe.load import RecipeError, build_recipe, load_toml, lower_layer_paths

    layer = read_layer(path)
    values = [] if layer.error else [layer.values]
    try:
        return build_recipe([load_toml(p) for p in lower_layer_paths(path)] + values)
    except RecipeError:  # an invalid folder or clip layer: the camera overrides fall back to the defaults
        return build_recipe([])


def _raw(path: Path, every_frame: bool, check: Callable[[], None]) -> ClipMotion | None:
    from mcrawsome.io.clip import Clip
    from mcrawsome.motion.raw_gyro import raw_gyro
    from mcrawsome.pipeline.stabilise import camera_profile

    with Clip(path, metadata_only=True) as clip:
        source = raw_gyro(clip, link_frames=None if every_frame else _LINK_FRAMES)
        if source is None:
            return None
        stamps = clip.timestamps_ns
        count = len(stamps) if every_frame else min(len(stamps), _LINK_FRAMES)
        frames = []
        for index in range(count):
            if index % 64 == 0:
                check()
            frames.append(clip.frame_timing(index))
        profile = camera_profile(clip, _recipe(path))
        fps = clip.fps
        first = clip.frame_metadata(0)
    gyro, _ = source.picture_log(profile)  # blur is the picture's: OIS lens shifts included
    starts = source.frame_times_s
    exposure = np.array([frame.exposure_time_ns for frame in frames], dtype=np.float64) / 1e9
    if not every_frame:
        exposure = np.full(len(stamps), exposure[0])
    focus = None
    if every_frame:
        focus = np.array([np.nan if f.focus_distance is None else f.focus_distance for f in frames])
    offset = source.sync_s(profile)
    readout = (profile.readout_ms if profile.readout_ms is not None else 0.4 / fps * 1000) / 1000
    centres = starts + exposure / 2 + readout / 2 + offset
    duration = float(stamps[-1] - stamps[0]) / 1e9 + 1 / fps
    return _motion(
        gyro,
        starts[0] + offset,
        duration,
        centres,
        exposure,
        profile.focal_px or 0.78 * first.width,
        fps,
        focus,
    )


def _video(path: Path, every_frame: bool) -> ClipMotion | None:
    from mcrawsome.io.directlog import probe_direct_log, read_gyro
    from mcrawsome.output.ffmpeg import ffprobe_for, find_ffmpeg

    ffmpeg, ffprobe = find_ffmpeg()
    info = probe_direct_log(ffprobe, path)
    gyro, device = read_gyro(ffmpeg, info)
    if gyro is None:
        return None
    return video_motion(gyro, device, info, ffprobe_for(ffmpeg) if every_frame else None)


def video_motion(
    gyro: GyroLog, device: dict[str, str], info: DirectLogInfo, ffprobe: Path | None
) -> ClipMotion:
    """Direct Log motion from telemetry already read (the library scan reads it anyway). With `ffprobe`,
    frames keep their recorded presentation times; without it they are spaced at the nominal rate."""
    from mcrawsome.camera.profile import CameraProfile, find_profile, focal_from_metadata
    from mcrawsome.io.directlog import camera_from_minf

    fps = float(info.fps)
    if ffprobe is not None:
        from mcrawsome.io.video_timing import presentation_times

        starts = presentation_times(ffprobe, info.path).astype(np.float64) / 1e9
    else:
        starts = np.arange(info.frame_count, dtype=np.float64) / fps
    if len(starts) == 0:
        raise ValueError("the gyro log does not cover any frame: the video has none")
    model, equivalent = camera_from_minf(device.get("MINF", ""))
    profile = find_profile(model, None, info.width, info.height, fps) or CameraProfile(name="unknown")
    focal = profile.focal_px
    if focal is None:
        focal = (
            focal_from_metadata(equivalent, info.width, info.width * 3 // 4)
            if equivalent
            else 0.78 * info.width
        )
    exposure = np.full(len(starts), 0.5 / fps)
    offset = (profile.gpmf_sync_ms or 0) / 1000
    readout = (profile.readout_ms if profile.readout_ms is not None else 0.4 / fps * 1000) / 1000
    centres = starts + exposure / 2 + readout / 2 + offset
    duration = float(starts[-1] - starts[0]) + 1 / fps
    motion = _motion(gyro, float(starts[0]) + offset, duration, centres, exposure, focal, fps, None)
    return replace(motion, note=LOG_EXPOSURE_NOTE)


def _motion(
    gyro: GyroLog,
    first_start: float,
    duration: float,
    centres: Array,
    exposure: Array,
    focal_px: float,
    fps: float,
    focus: Array | None,
) -> ClipMotion:
    return ClipMotion(
        rates=rates(gyro, first_start, first_start + duration),
        centres=np.asarray(centres, dtype=np.float64),
        exposure_s=np.asarray(exposure, dtype=np.float64),
        focal_px=float(focal_px),
        first_start=float(first_start),
        duration_s=float(duration),
        frame_rate=float(fps),
        focus=focus,
    )


class BlurStretch(BaseModel):
    start_s: float = Field(description="Seconds after the first frame starts")
    duration_s: float
    peak_px: float


class MotionReport(BaseModel):
    """What the gyro log says about a clip: its movement, shake blur and a suggested trim."""

    movement: Movement | None = None
    shares: dict[Movement, float] = Field(default_factory=dict, description="Share of seconds per movement")
    blur_median_px: float | None = None
    blur_p95_px: float | None = None
    blurred: list[BlurStretch] = Field(
        default_factory=list,
        description="Stretches of half a second or more with shake blur of 20 px or more",
    )
    trim: TrimSuggestion | None = None
    notes: list[str] = Field(default_factory=list)


def motion_report(path: Path, stop: threading.Event) -> MotionReport:
    """The clip's MotionReport; empty, with a note, when it has no usable gyro log."""
    from mcrawsome.analysis.diagnostics import _check

    motion = load(path, check=lambda: _check(stop))
    if motion is None:
        return MotionReport(notes=["Movement, blur and trim suggestions need the clip's gyro log"])
    _check(stop)
    blur = motion.blur()
    finite = blur[np.isfinite(blur)]
    summary = motion.summary()
    stretches = [
        BlurStretch(
            start_s=round(max(0.0, float(motion.centres[first] - motion.first_start)), 3),
            duration_s=round(count / motion.frame_rate, 3),
            peak_px=round(peak, 1),
        )
        for first, count, peak in blurred_stretches(blur, motion.frame_rate)
    ]
    return MotionReport(
        movement=summary.movement,
        shares=summary.shares,
        blur_median_px=round(float(np.median(finite)), 1) if len(finite) else None,
        blur_p95_px=round(float(np.percentile(finite, 95)), 1) if len(finite) else None,
        blurred=stretches,
        trim=motion.trim(),
        notes=[motion.note] if motion.note else [],
    )


def measured_keywords(path: Path) -> tuple[str, ...]:
    """Resolve keywords measured from the gyro log: the movement, and Motion blur when the clip has a
    second or more of sustained shake blur. Empty without a usable log."""
    from mcrawsome.analysis.motion_summary import MOVEMENT_LABELS

    motion = load(path, every_frame=False)
    if motion is None:
        return ()
    facts = motion.facts()
    movement = facts["movement"]
    words = [MOVEMENT_LABELS[movement]] if isinstance(movement, str) and movement in MOVEMENT_LABELS else []
    blurred = facts["blurred_s"]
    if isinstance(blurred, float) and blurred >= 1.0:
        words.append("Motion blur")
    return tuple(words)
