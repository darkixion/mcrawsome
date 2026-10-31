import json
import tracemalloc
from pathlib import Path

import pytest
from pydantic import ValidationError

from mcrawsome.io.clip import Clip
from mcrawsome.io.container import ContainerWriter

DATA = Path(__file__).parent.parent / "data"


def recording(path: Path) -> Path:
    container = json.loads((DATA / "container_metadata_174122.json").read_text())
    frame = json.loads((DATA / "frame_metadata_174122.json").read_text())
    with ContainerWriter(path, container) as writer:
        for index in range(3):
            metadata = frame | {
                "exposureTime": 10_000_000 + index * 500_000,
                "recvdTimestampMs": str(1000 + index * 42),
                "focusDistance": None if index == 0 else index * 0.25,
                "orientation": index,
                "lensShadingMap": [[1.0] * 4096] * 4,
            }
            writer.add_frame(1_000_000_000 + index * 41_666_667, b"unused", json.dumps(metadata).encode())
    return path


def test_timing_preserves_recorded_geometry_exposure_focus_and_received_time(tmp_path: Path) -> None:
    with Clip(recording(tmp_path / "capture.mcraw")) as clip:
        for index in range(clip.frame_count):
            full = clip.frame_metadata(index)
            timing = clip.frame_timing(index)
            for name in type(timing).model_fields:
                assert getattr(timing, name) == getattr(full, name)


def test_timing_scan_does_not_materialise_lens_shading_arrays(tmp_path: Path) -> None:
    with Clip(recording(tmp_path / "capture.mcraw")) as clip:
        tracemalloc.start()
        try:
            full = clip.frame_metadata(0)
            full_peak = tracemalloc.get_traced_memory()[1]
            del full
            tracemalloc.reset_peak()
            timing = clip.frame_timing(0)
            timing_peak = tracemalloc.get_traced_memory()[1]
            assert timing.received_ms == 1000
            assert timing_peak < full_peak / 4
        finally:
            tracemalloc.stop()


def test_gyro_calibration_keeps_each_frames_recorded_exposure(tmp_path: Path) -> None:
    import numpy as np

    from mcrawsome.pipeline.calibrate import read_recording

    capture = read_recording(recording(tmp_path / "capture.mcraw"), workers=1)
    np.testing.assert_allclose(capture.exposure_s, [0.010, 0.0105, 0.011], rtol=0, atol=1e-12)


def test_timing_still_rejects_a_missing_received_timestamp(tmp_path: Path) -> None:
    from mcrawsome.io.metadata import FrameTiming

    with Clip(recording(tmp_path / "capture.mcraw")) as clip:
        raw = clip.raw_frame_metadata(0)
    del raw["recvdTimestampMs"]
    with pytest.raises(ValidationError, match="recvdTimestampMs"):
        FrameTiming.model_validate_json(json.dumps(raw))
