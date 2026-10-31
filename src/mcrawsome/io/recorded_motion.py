"""Motion that MotionCam 5.1 and later records inside the .mcraw: the gyroscope and accelerometer, and the
lens shifts of optical image stabilisation (OIS), all on the frames' own sensor clock."""

from __future__ import annotations

from dataclasses import dataclass
from typing import BinaryIO

import numpy as np
import numpy.typing as npt

from mcrawsome.io.container import ContainerIndex, ItemType, read_motion


@dataclass(frozen=True)
class Samples:
    times_ns: npt.NDArray[np.int64]
    values: npt.NDArray[np.float64]  # a row per sample: x, y, z (x, y for lens shifts)


@dataclass(frozen=True)
class RecordedMotion:
    gyro: Samples  # rad/s on the device's own axes
    accelerometer: Samples | None  # m/s2, gravity included, on the device's own axes
    ois: Samples | None  # lens shifts in pixels of the sensor's pre-correction active array


def _samples(f: BinaryIO, offsets: tuple[int, ...], item_type: ItemType) -> Samples | None:
    raw = read_motion(f, offsets, item_type)
    if not len(raw):
        return None
    names = ("x", "y") if item_type == ItemType.OIS_DATA else ("x", "y", "z")
    values = np.stack([raw[name].astype(np.float64) for name in names], -1)
    return Samples(raw["t"].astype(np.int64), values)


def read_recorded_motion(f: BinaryIO, index: ContainerIndex) -> RecordedMotion | None:
    """The recording's motion, or None when it has no gyro samples."""
    gyro = _samples(f, index.gyro, ItemType.GYRO_DATA)
    if gyro is None or len(gyro.times_ns) < 2:
        return None
    return RecordedMotion(
        gyro,
        _samples(f, index.accelerometer, ItemType.ACCELEROMETER_DATA),
        _samples(f, index.ois, ItemType.OIS_DATA),
    )
