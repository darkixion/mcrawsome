"""Exposure normalisation (deflicker): per-frame gains that even out exposure jumps and flicker between
frames (holy-grail timelapses, misbehaving auto exposure) while keeping slow changes.

Each frame's brightness is the mean of its unclipped green photosites above black (every 4th row and
column). A frame with too few usable samples takes its brightness from its metadata exposure (ISO x
shutter) relative to the others. The gain brings each frame's brightness (in stops) towards the moving
average over a window of frames, by `strength`.
"""

from __future__ import annotations

import atexit
import contextlib
import math
from collections.abc import Sequence
from functools import partial
from pathlib import Path

import numpy as np
import numpy.typing as npt

from mcrawsome.develop.crop import CropBox, crop_data
from mcrawsome.develop.raw import Levels, levels
from mcrawsome.io.clip import Clip, close_all
from mcrawsome.pipeline.pool import bounded_imap, worker_pool

_SITES = ((0, 0), (0, 1), (1, 0), (1, 1))
_SUBSAMPLE = 4
_MIN_SHARE = 0.01  # of the green samples checked, the least that must be usable
_CLIP_MARGIN = 2.0


def frame_brightness(raw: npt.NDArray[np.uint16], cfa: str, blacks: Levels, white: float) -> float | None:
    """The mean of the frame's unclipped green photosites above black, or None when fewer than 1% of the
    samples checked are usable (or they average at or below black)."""
    values, usable = [], 0
    total = 0
    for i, (y, x) in enumerate(_SITES):
        if cfa[i] != "g":
            continue
        site = raw[y::_SUBSAMPLE, x::_SUBSAMPLE].astype(np.float32)
        total += site.size
        kept = site[site < white - _CLIP_MARGIN] - np.float32(blacks[i])
        usable += kept.size
        values.append(kept)
    if usable < _MIN_SHARE * total:
        return None
    mean = float(np.concatenate(values).mean())
    return mean if mean > 0 else None


def window_frames(seconds: float, fps: float) -> int:
    """The smoothing window in frames: odd, at least 1."""
    frames = max(1, round(seconds * fps))
    return frames if frames % 2 else frames + 1


def normalising_gains(
    brightness: Sequence[float | None], exposures: Sequence[float], window: int, strength: float
) -> list[float]:
    """Per-frame gains: each frame's brightness in stops moves towards its moving average over `window`
    frames (mirrored at the ends) by `strength`. A frame without a brightness uses its metadata exposure,
    offset by the median difference between the two over the frames that have both."""
    if strength <= 0 or not brightness:
        return [1.0] * len(brightness)
    log_exposure = np.log2(np.maximum(np.asarray(exposures, np.float64), 1e-12))
    known = np.array([b is not None for b in brightness])
    measured = np.array([math.log2(b) if b is not None else 0.0 for b in brightness])
    offset = float(np.median(measured[known] - log_exposure[known])) if known.any() else 0.0
    stops = np.where(known, measured, log_exposure + offset)
    half = window // 2
    padded = np.pad(stops, half, mode="symmetric") if half else stops
    smooth = np.convolve(padded, np.ones(window) / window, mode="valid")
    return [float(g) for g in 2.0 ** (strength * (smooth - stops))]


_clips: dict[Path, Clip] = {}


@atexit.register
def _close_clips() -> None:
    clips = list(_clips.values())
    _clips.clear()
    close_all(clips)


def _measure(
    clip_path: Path,
    black: float | Levels | None,
    white: float | None,
    crop: tuple[int, int, int, int] | None,
    index: int,
    *,
    quad_bayer_remosaic: bool,
) -> tuple[float | None, float]:
    if clip_path in _clips and _clips[clip_path].quad_bayer_remosaic != quad_bayer_remosaic:
        _clips.pop(clip_path).close()
    if clip_path not in _clips:  # one open clip per worker process
        _clips[clip_path] = Clip(clip_path, quad_bayer_remosaic=quad_bayer_remosaic)
    clip = _clips[clip_path]
    raw = clip.raw_frame(index)
    data = crop_data(raw.data, CropBox(*crop)) if crop is not None else raw.data
    blacks, top = levels(raw.metadata, black, white)
    exposure = raw.metadata.iso * raw.metadata.exposure_time_ns
    return frame_brightness(data, clip.metadata.sensor_arrangement, blacks, top), exposure


def measure_gains(
    clip_path: Path,
    strength: float,
    seconds: float,
    workers: int,
    *,
    black_level: float | Levels | None = None,
    white_level: float | None = None,
    crop: tuple[int, int, int, int] | None = None,
    quad_bayer_remosaic: bool | None = None,
) -> list[float]:
    """The gain for every recorded frame of the clip (a pass over all of its frames)."""
    with Clip(clip_path, quad_bayer_remosaic=quad_bayer_remosaic) as clip:
        count, fps = clip.frame_count, clip.fps
        enabled = clip.quad_bayer_remosaic
    measure = partial(_measure, clip_path, black_level, white_level, crop, quad_bayer_remosaic=enabled)
    with (
        worker_pool(workers) as pool,
        contextlib.closing(bounded_imap(pool, measure, range(count), window=workers * 2)) as results,
    ):
        found = list(results)
    brightness = [b for b, _ in found]
    exposures = [e for _, e in found]
    return normalising_gains(brightness, exposures, window_frames(seconds, fps), strength)


def deflicker_stops(gains: Sequence[float]) -> float:
    """The largest correction, in stops, for the sidecar."""
    return round(max((abs(math.log2(g)) for g in gains), default=0.0), 3)
