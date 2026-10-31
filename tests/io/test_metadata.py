import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from mcrawsome.io.metadata import FrameMetadata, parse_container_metadata

DATA = Path(__file__).parent.parent / "data"


def test_parses_container_metadata() -> None:
    m = parse_container_metadata(json.loads((DATA / "container_metadata_pixel10.json").read_text()))
    assert m.sensor_arrangement == "gbrg"
    assert m.white_level == 4095.0
    assert m.device_model == "pixel 10 pro xl"
    assert m.audio_sample_rate == 48000
    assert m.audio_channels == 2
    assert len(m.forward_matrix_2) == 9


def test_rejects_unknown_bayer_layout() -> None:
    raw = json.loads((DATA / "container_metadata_pixel10.json").read_text())
    raw["sensorArrangment"] = "rgbw"
    with pytest.raises(ValidationError):
        parse_container_metadata(raw)


def test_frame_metadata_coerces_string_numbers() -> None:
    fm = FrameMetadata.model_validate(
        {
            "width": 4,
            "height": 2,
            "originalWidth": 8,
            "originalHeight": 6,
            "orientation": 2,
            "compressionType": 7,
            "asShotNeutral": [0.5, 1, 0.6],
            "dynamicBlackLevel": [64, 64, 64, 64],
            "dynamicWhiteLevel": 1023,
            "iso": 36,
            "exposureTime": 20824816.0,
            "lensShadingMap": [[1.0] * 4] * 4,
            "lensShadingMapWidth": 2,
            "lensShadingMapHeight": 2,
            "recvdTimestampMs": "1791132023049",
        }
    )
    assert fm.received_ms == 1791132023049
    assert fm.as_shot_neutral == (0.5, 1.0, 0.6)


def _approved() -> dict[str, object]:
    return json.loads((DATA / "container_metadata_174122.json").read_text())  # type: ignore[no-any-return]


def test_lens_and_camera_details() -> None:
    m = parse_container_metadata(_approved())
    assert m.focal_lengths == pytest.approx([25.7653], abs=1e-3)
    assert m.apertures == pytest.approx([1.7], abs=1e-6)
    assert m.sensor_orientation == 90
    assert m.colour_illuminant_1 == "standarda"
    assert m.colour_illuminant_2 == "d65"
    assert m.calibration_matrix_1 is not None
    assert m.calibration_matrix_1[0] == pytest.approx(0.99465, abs=1e-5)
    assert m.camera_id is not None


def test_older_files_without_details_still_parse() -> None:
    m = parse_container_metadata(json.loads((DATA / "container_metadata_pixel10.json").read_text()))
    assert m.focal_lengths == []
    assert m.calibration_matrix_1 is None
    assert m.app_version is None


@pytest.mark.parametrize("accurate", [True, False])
def test_capture_clock_flags_are_retained_recorded_contract(accurate: bool) -> None:
    raw = _approved()
    raw["timestampSource"] = 1
    raw["extraData"] = {"useAccurateTimestamp": accurate}
    parsed = parse_container_metadata(raw)
    assert parsed.timestamp_source == 1
    assert parsed.use_accurate_timestamp is accurate


def test_absent_capture_clock_flags_remain_unknown_recorded_contract() -> None:
    raw = _approved()
    raw.pop("timestampSource", None)
    raw["extraData"] = {}
    parsed = parse_container_metadata(raw)
    assert parsed.timestamp_source is None
    assert parsed.use_accurate_timestamp is None


def test_frame_details() -> None:
    fm = FrameMetadata.model_validate(json.loads((DATA / "frame_metadata_174122.json").read_text()))
    assert fm.focus_distance == pytest.approx(5.7388, abs=1e-3)
    assert fm.exposure_compensation == 0
    assert len(fm.noise_profile) == 8
    assert fm.is_binned is False


def test_device_and_app_version_from_motioncam_build_keys() -> None:
    raw = _approved()
    extra = dict(raw["extraData"])  # type: ignore[call-overload]
    extra["postProcessSettings"] = {
        "metadata": {
            "app.release.type": "pro",
            "build.manufacturer": "Google",
            "build.model": "Pixel 10 Pro XL",
            "build.device": "mustang",
            "version.major": "5",
            "version.minor": "0",
            "version.build": "9",
        }
    }
    m = parse_container_metadata({**raw, "extraData": extra})
    assert (m.manufacturer, m.build_model, m.build_device) == ("Google", "Pixel 10 Pro XL", "mustang")
    assert m.app_version == "5.0.9"
    assert m.app_release == "pro"


@pytest.mark.parametrize(
    ("latitude", "longitude", "altitude", "time", "expected"),
    [
        (51.5, -0.12, -3.25, "2026-10-20T12:00:00Z", (51.5, -0.12, -3.25)),
        (-33.9, 151.2, None, "", (-33.9, 151.2, None)),
        (0, 0, 0, "", None),
        (0, 0, 0, "2026-10-20T12:00:00Z", (0, 0, 0)),
        (91, 0, 0, "", None),
        (0, -181, 0, "", None),
        (True, 1, 0, "", None),
        (float("nan"), 1, 0, "", None),
        (51, 1, float("inf"), "", (51, 1, None)),
        (10**1000, 1, 0, "", None),
    ],
)
def test_optional_embedded_gps_is_validated_without_rejecting_the_clip(
    latitude: object,
    longitude: object,
    altitude: object,
    time: str,
    expected: tuple[float, float, float | None] | None,
) -> None:
    raw = _approved()
    raw["extraData"] = {
        "postProcessSettings": {
            "gpsLatitude": latitude,
            "gpsLongitude": longitude,
            "gpsAltitude": altitude,
            "gpsTime": time,
        }
    }
    result = parse_container_metadata(raw)
    assert result.gps == expected
    assert result.gps_time == (time or None)
    assert not any(key.startswith("gps") for key in result.app_settings)


@pytest.mark.parametrize("accurate", [True, False])
def test_capture_clock_flags_are_retained(accurate: bool) -> None:
    raw = _approved()
    raw["timestampSource"] = 1
    raw["extraData"] = {"useAccurateTimestamp": accurate}
    parsed = parse_container_metadata(raw)
    assert parsed.timestamp_source == 1
    assert parsed.use_accurate_timestamp is accurate


def test_absent_capture_clock_flags_remain_unknown() -> None:
    raw = _approved()
    raw.pop("timestampSource", None)
    raw["extraData"] = {}
    parsed = parse_container_metadata(raw)
    assert parsed.timestamp_source is None
    assert parsed.use_accurate_timestamp is None
