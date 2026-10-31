"""Brightness of the developed picture on a display, from a sample of frames, through the neutral
display rendering (colour/display.py), measured at 1920 pixels wide."""

from __future__ import annotations

from dataclasses import dataclass
from functools import partial

import cv2
import numpy as np
import numpy.typing as npt

from mcrawsome.colour.cube import Table, apply_cube
from mcrawsome.colour.display import display_table
from mcrawsome.pipeline.render import FrameJob, iter_frames

MAX_SAMPLES = 48
MEASURE_WIDTH = 1920

# Median luma code values: below DARK or above BRIGHT is worth a warning.
SDR_DARK, SDR_TYPICAL, SDR_BRIGHT = 0.25, (0.3, 0.5), 0.6
_BT709_LUMA = np.array([0.2126, 0.7152, 0.0722])


def sample_indices(frame_count: int, samples: int = MAX_SAMPLES) -> list[int]:
    count = min(samples, frame_count)
    return sorted({round(i * (frame_count - 1) / max(count - 1, 1)) for i in range(count)})


@dataclass(frozen=True)
class SdrLight:
    """Brightness of the developed picture on a display: BT.709 luma code values (0 to 1)."""

    median_code: float
    p90_code: float


def frame_code(frame: npt.NDArray[np.uint16], table: Table) -> tuple[float, float]:
    """Median and 90th percentile of the display code values' luma, through the display table."""
    height, width = frame.shape[:2]
    small = frame.astype(np.float32) / 65535.0
    if width > MEASURE_WIDTH:
        size = (MEASURE_WIDTH, round(height * MEASURE_WIDTH / width))
        small = np.asarray(cv2.resize(small, size, interpolation=cv2.INTER_AREA), dtype=np.float32)
    y = apply_cube(table, small.reshape(-1, 3)) @ _BT709_LUMA
    return float(np.median(y)), float(np.percentile(y, 90))


def measure_sdr(job: FrameJob, frame_count: int, workers: int) -> SdrLight:
    """Up to MAX_SAMPLES evenly spaced frames of an ACEScct job; the display table is applied in the
    workers."""
    indices = sample_indices(frame_count)
    finish = partial(frame_code, table=display_table())
    codes = list(iter_frames(job, frame_count, workers, indices, finish=finish))
    return SdrLight(float(np.mean([c[0] for c in codes])), float(np.mean([c[1] for c in codes])))


def sdr_brightness_warning(light: SdrLight) -> str | None:
    typical = f"{SDR_TYPICAL[0]} to {SDR_TYPICAL[1]}"
    if light.median_code < SDR_DARK:
        return (
            f"the picture looks dark (median code {light.median_code:.2f}; {typical} is typical): "
            "try --develop.exposure +1"
        )
    if light.median_code > SDR_BRIGHT:
        return (
            f"the picture looks very bright (median code {light.median_code:.2f}; {typical} is typical): "
            "try --develop.exposure -1"
        )
    return None
