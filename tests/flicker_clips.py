"""Synthetic clips whose brightness changes while the recorded exposure does not."""

import json
from pathlib import Path

from conftest import _DATA, SYNTHETIC_STEP, _payload

from mcrawsome.io.container import ContainerWriter


def flicker_clip(path: Path, levels: list[int]) -> Path:
    """A clip whose frames have these brightness levels, all recorded at one exposure: flicker that the
    metadata does not show (a light source, or a timelapse ramp's steps between recorded values)."""
    meta = json.loads((_DATA / "container_metadata_174122.json").read_text())
    frame = json.loads((_DATA / "frame_metadata_174122.json").read_text())
    frame |= {"width": 128, "height": 64, "originalWidth": 128, "originalHeight": 64}
    frame |= {"rowStride": 256, "orientation": 0}
    with ContainerWriter(path, meta) as writer:
        for i, level in enumerate(levels):
            stamp = i * SYNTHETIC_STEP
            writer.add_frame(stamp, _payload(level), json.dumps(frame | {"timestamp": stamp}).encode())
    return path
