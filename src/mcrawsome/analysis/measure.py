"""Measurements on a sample of frames, made before the look (scene-linear AP1 at exposure 0).

- **Key:** the log-average luminance of each frame below its 98th percentile, so a lamp or the sun does
  not drag the exposure down.
- **Clipping:** the share of photosites at sensor white (RAW) or of pixels at the encoding's top
  (video).
- **Noise:** Immerkaer's fast estimate on full-resolution luminance in the mid-tones of a central crop,
  relative to the signal there. Shot noise grows with the square root of the signal, so the noise at
  the scene level that becomes mid-grey after the exposure push follows from it.
"""

from __future__ import annotations

import dataclasses
import math
from collections.abc import Sequence
from dataclasses import dataclass
from functools import partial
from pathlib import Path

import cv2
import numpy as np
import numpy.typing as npt

from mcrawsome.colour.transfer import acescct_decode
from mcrawsome.io.clip import Clip
from mcrawsome.io.directlog import is_video_input
from mcrawsome.pipeline.measure import sample_indices
from mcrawsome.pipeline.render import FrameJob, iter_frames, video_linear
from mcrawsome.progress import Progress

Image = npt.NDArray[np.float32]
_AP1_Y = np.array([0.2722287, 0.6740818, 0.0536895], np.float32)
_KEY_WIDTH = 960
_NOISE_CROP = (540, 960)  # rows, columns at full resolution
_IMMERKAER = np.array([[1, -2, 1], [-2, 4, -2], [1, -2, 1]], np.float32)
_EPS = 1e-4
MID_GREY = 0.18


@dataclass(frozen=True)
class FrameStats:
    log_sum: float  # sum of log luminance over the key's pixels
    pixels: int
    clipped: float  # share of this frame that is clipped
    noise_rel: float  # noise over signal in the mid-tones
    noise_level: float  # the mid-tones' mean luminance


@dataclass(frozen=True)
class SampleStats:
    key: float
    clipped_share: float
    noise_rel: float
    noise_level: float
    frames: int

    def noise_mid_grey(self, exposure: float) -> float:
        """Relative noise at the scene level that `exposure` stops turn into mid-grey."""
        level = MID_GREY / 2.0**exposure
        return self.noise_rel * math.sqrt(self.noise_level / level) if self.noise_level > 0 else 0.0


def frame_stats(linear: Image, clip_level: float | None) -> FrameStats:
    """`clip_level`: for video, the linear level of the encoding's top code (None for RAW, whose
    clipping is counted from the photosites instead)."""
    height, width = linear.shape[:2]
    small = linear
    if width > _KEY_WIDTH:
        size = (_KEY_WIDTH, round(height * _KEY_WIDTH / width))
        small = np.asarray(cv2.resize(linear, size, interpolation=cv2.INTER_AREA), dtype=np.float32)
    y = np.maximum(small @ _AP1_Y, 0.0)
    kept = y[y <= np.percentile(y, 98)]
    log_sum = float(np.log(kept + _EPS).sum())
    clipped = float((linear.max(-1) >= clip_level).mean()) if clip_level is not None else 0.0
    rows, cols = min(_NOISE_CROP[0], height), min(_NOISE_CROP[1], width)
    top, left = (height - rows) // 2, (width - cols) // 2
    crop = linear[top : top + rows, left : left + cols] @ _AP1_Y
    response = np.abs(cv2.filter2D(crop, -1, _IMMERKAER, borderType=cv2.BORDER_REFLECT))[1:-1, 1:-1]
    smooth = cv2.blur(crop, (7, 7))[1:-1, 1:-1]
    mid = (smooth > 0.02) & (smooth < 0.5)
    if mid.sum() < 1000:
        return FrameStats(log_sum, kept.size, clipped, 0.0, 0.0)
    sigma = math.sqrt(math.pi / 2) / 6 * float(response[mid].mean())
    level = float(smooth[mid].mean())
    return FrameStats(log_sum, kept.size, clipped, sigma / level, level)


def combine(frames: Sequence[FrameStats]) -> SampleStats:
    pixels = sum(f.pixels for f in frames)
    key = math.exp(sum(f.log_sum for f in frames) / pixels) - _EPS if pixels else 0.0
    noisy = [f for f in frames if f.noise_level > 0]
    return SampleStats(
        key=max(key, 0.0),
        clipped_share=float(np.mean([f.clipped for f in frames])) if frames else 0.0,
        noise_rel=float(np.median([f.noise_rel for f in noisy])) if noisy else 0.0,
        noise_level=float(np.median([f.noise_level for f in noisy])) if noisy else 0.0,
        frames=len(frames),
    )


def mired_spread(ccts: Sequence[float]) -> float:
    """How far the white balance moves during a clip: the range of 1e6 / CCT."""
    mireds = [1e6 / cct for cct in ccts]
    return max(mireds) - min(mireds) if mireds else 0.0


SAMPLES = 24


def _from_encoded(frame: npt.NDArray[np.uint16], clip_level: float | None) -> FrameStats:
    linear = acescct_decode(frame.astype(np.float32) / np.float32(65535))
    return frame_stats(np.asarray(linear, np.float32), clip_level)


def _raw_clipped(clip_path: Path, indices: Sequence[int]) -> float:
    with Clip(clip_path) as clip:
        shares = []
        for index in indices:
            frame = clip.raw_frame(index)
            shares.append(float((frame.data >= frame.metadata.white_level - 2).mean()))
    return float(np.mean(shares)) if shares else 0.0


def measure_samples(
    job: FrameJob, frame_count: int, workers: int, progress: Progress | None = None
) -> SampleStats:
    """Up to SAMPLES evenly spaced frames, developed as the job would at exposure 0, before stabilisation
    or display rotation."""
    probe = dataclasses.replace(
        job, exposure_stops=0.0, target="acescct", plan_path=None, rotation=0, float_output=False
    )
    indices = sample_indices(frame_count, SAMPLES)
    clip_level = None
    if job.source == "video":
        top = video_linear(job, np.full((1, 1, 3), 65535, np.uint16))
        clip_level = 0.99 * float(np.max(top))
    finish = partial(_from_encoded, clip_level=clip_level)
    lights: list[FrameStats] = []
    for done, light in enumerate(iter_frames(probe, frame_count, workers, indices, finish=finish), start=1):
        lights.append(light)
        if progress:
            progress("measuring", done, len(indices))
    stats = combine(lights)
    if job.source == "raw":
        sources = [job.sources[i] for i in indices] if job.sources is not None else indices
        stats = dataclasses.replace(stats, clipped_share=_raw_clipped(job.clip_path, sources))
    return stats


def white_balance_spread(clip_path: Path) -> float:
    """Mired range of the as-shot white balance over a sample of a RAW clip's frames (0 for videos)."""
    from mcrawsome.camera.colour import camera_to_ap1

    if is_video_input(clip_path):
        return 0.0
    with Clip(clip_path) as clip:
        indices = sample_indices(clip.frame_count, SAMPLES)
        ccts = [camera_to_ap1(clip.metadata, clip.frame_metadata(i).as_shot_neutral)[1] for i in indices]
    return mired_spread(ccts)
