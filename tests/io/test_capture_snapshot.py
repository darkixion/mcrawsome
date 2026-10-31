import json
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from mcrawsome.io.capture import capture_snapshot
from mcrawsome.io.metadata import FrameMetadata, parse_container_metadata

_DATA = Path(__file__).parent.parent / "data"


def test_snapshot_preserves_capture_identity_units_and_first_frame_scope() -> None:
    container = parse_container_metadata(json.loads((_DATA / "container_metadata_174122.json").read_text()))
    frame = FrameMetadata.model_validate(json.loads((_DATA / "frame_metadata_174122.json").read_text()))
    value = capture_snapshot(container, frame, measured_fps=24.0)
    assert value.manufacturer == "Google"
    assert value.camera_model == "Pixel 10 Pro XL"
    assert value.camera_id == "0/9"
    assert value.aperture == pytest.approx(1.7)
    assert value.focal_equivalents_mm == pytest.approx((25.765306,))
    assert value.exposure_seconds == 0.020824816
    assert value.exposure_compensation_ev == 0.0
    assert value.focus_metres == pytest.approx(0.174253657)
    assert value.captured_utc is not None
    assert value.captured_utc.isoformat() == "2026-10-04T16:41:23.080000+00:00"
    assert value.recorded_size == (3840, 2160)
    assert value.sensor_size == (4032, 3024)
    with pytest.raises(FrozenInstanceError):
        value.camera_id = "other"  # type: ignore[misc]


def test_optional_invalid_values_and_ambiguous_aperture_are_omitted() -> None:
    container = parse_container_metadata(json.loads((_DATA / "container_metadata_174122.json").read_text()))
    frame = FrameMetadata.model_validate(json.loads((_DATA / "frame_metadata_174122.json").read_text()))
    container = container.model_copy(
        update={
            "manufacturer": "bad\x00name",
            "build_model": "bad\ud800",
            "apertures": [1.4, 2.8],
            "exposure_compensation_step": (1, 0),
        }
    )
    frame = frame.model_copy(
        update={"iso": float("nan"), "exposure_time_ns": -1.0, "received_ms": 10**100, "focus_distance": 0.0}
    )
    value = capture_snapshot(container, frame, measured_fps=None)
    assert value.manufacturer is None
    assert value.camera_model == "pixel 10 pro xl"
    assert value.aperture is None
    assert value.apertures == (1.4, 2.8)
    assert value.iso is None
    assert value.exposure_seconds is None
    assert value.exposure_compensation_ev is None
    assert value.captured_utc is None
    assert value.focus_infinity
    assert value.focus_metres is None
    assert value.measured_fps is None


def test_gps_fix_and_reported_time_survive_without_inventing_height_or_timezone() -> None:
    raw = json.loads((_DATA / "container_metadata_174122.json").read_text())
    raw["extraData"]["postProcessSettings"].update(
        {
            "gpsLatitude": 51.5,
            "gpsLongitude": -0.12,
            "gpsAltitude": None,
            "gpsTime": " 2026-10-20T12:00:00Z ",
        }
    )
    container = parse_container_metadata(raw)
    frame = FrameMetadata.model_validate(json.loads((_DATA / "frame_metadata_174122.json").read_text()))
    result = capture_snapshot(container, frame, measured_fps=24)
    assert result.gps == (51.5, -0.12, None)
    assert result.gps_time == "2026-10-20T12:00:00Z"
    assert result.captured_utc != result.gps_time
    invalid = container.model_copy(update={"gps": (float("nan"), 1, 0), "gps_time": "bad\x00time"})
    result = capture_snapshot(invalid, frame, measured_fps=24)
    assert result.gps is None
    assert result.gps_time is None
