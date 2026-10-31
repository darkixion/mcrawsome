"""Synthetic clips whose frames MotionCam marks as Quad Bayer (needRemosaic)."""

import json
from pathlib import Path
from typing import Literal

import numpy as np
import numpy.typing as npt
from conftest import _DATA, SYNTHETIC_STEP, _payload

from mcrawsome import _raw
from mcrawsome.io.container import ContainerWriter, read_frame_record


def quad_clip(path: Path, frames: int = 3, pattern: Literal["rggb", "bggr", "grbg", "gbrg"] = "rggb") -> Path:
    """A 128x64 clip of `frames` frames marked needRemosaic, as a Quad Bayer sensor at full resolution."""
    meta = json.loads((_DATA / "container_metadata_174122.json").read_text())
    meta["sensorArrangment"] = pattern
    frame = json.loads((_DATA / "frame_metadata_174122.json").read_text())
    frame |= {"width": 128, "height": 64, "originalWidth": 128, "originalHeight": 64}
    frame |= {"rowStride": 256, "orientation": 0, "needRemosaic": True}
    with ContainerWriter(path, meta) as writer:
        for i in range(frames):
            stamp = i * SYNTHETIC_STEP
            writer.add_frame(stamp, _payload(120 + 50 * i), json.dumps(frame | {"timestamp": stamp}).encode())
    return path


def recorded_values(path: Path, index: int) -> npt.NDArray[np.uint16]:
    """A frame's values as recorded (Quad Bayer), without the remosaic."""
    from mcrawsome.io.clip import Clip

    with Clip(path) as clip:
        offset = clip._index.frames[index].offset
        payload, meta = read_frame_record(clip._f, offset)
    frame = json.loads(meta)
    values: npt.NDArray[np.uint16] = _raw.decode(
        payload, frame["width"], frame["height"], frame["compressionType"]
    )
    return values
