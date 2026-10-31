from dataclasses import replace
from datetime import UTC, datetime

from mcrawsome.io.capture import CaptureMetadata
from mcrawsome.resolve.metadata import capture_fields


def sample() -> CaptureMetadata:
    return CaptureMetadata(
        manufacturer="Recorded maker",
        camera_model="Recorded model",
        camera_id="0/9",
        aperture=1.7,
        apertures=(1.7,),
        focal_equivalents_mm=(25.7653,),
        iso=36,
        exposure_seconds=1 / 48,
        exposure_compensation_ev=0.0,
        focus_metres=0.25,
        measured_fps=24.0,
        recorded_size=(3840, 2160),
        sensor_size=(4032, 3024),
        is_binned=False,
        app_version="5.0.9",
        app_release="pro",
        device_code="test-device",
    )


def test_capture_mapping_has_explicit_units_and_keeps_production_fields_separate() -> None:
    result = capture_fields(sample(), white_balance=(5168.176, -15.6265), sample_rate=48000, channels=2)
    assert {
        key: result[key] for key in (96, 97, 99, 107, 119, 120, 134, 138, 104, 108, 109, 122, 220, 213)
    } == {
        96: "Recorded maker",
        97: "Recorded model",
        99: "0/9",
        107: "36",
        119: "F-stop",
        120: "1.7",
        134: "180",
        138: "0 EV",
        104: "24",
        108: "5168.176",
        109: "-15.6265",
        122: "0.25 m",
        220: "48",
        213: "WAV",
    }
    assert result[135] == "1/48"
    assert "35 mm equivalent" in result[118]
    assert "25.7653" in result[118]
    assert result[129] == "3840 x 2160 px within 4032 x 3024 px; not binned"
    assert "first recorded frame" in result[100]
    assert "MotionCam Pro 5.0.9" in result[100]
    assert "2 channels" in result[211]
    assert not (
        {64, 65, 66, 67, 68, 74, 77, 80, 88, 98, 112, 116, 117, 121, 149, 150, 151, 224, 227} & result.keys()
    )


def test_empty_capture_does_not_invent_values_and_audio_uses_actual_output() -> None:
    result = capture_fields(CaptureMetadata(), white_balance=None, sample_rate=0, channels=0)
    assert result == {}
    result = capture_fields(sample(), white_balance=None, sample_rate=44100, channels=1)
    assert result[220] == "44.1"
    assert "1 channel" in result[211]
    assert 108 not in result
    assert 109 not in result
    silent = capture_fields(sample(), white_balance=None, sample_rate=48000, channels=0)
    assert 220 not in silent
    assert 213 not in silent
    assert 211 not in silent


def test_infinity_multiple_apertures_and_missing_capture_fps_are_explicit() -> None:
    value = replace(
        sample(),
        aperture=None,
        apertures=(1.4, 2.8),
        focus_metres=None,
        focus_infinity=True,
        measured_fps=None,
    )
    result = capture_fields(value, white_balance=None, sample_rate=0, channels=0)
    assert result[122] == "Infinity"
    assert 119 not in result
    assert 120 not in result
    assert "active aperture unknown" in result[118]
    assert 104 not in result
    assert 134 not in result


def test_date_is_explicit_utc_and_invalid_white_balance_is_not_exported() -> None:
    value = replace(sample(), captured_utc=datetime(2026, 10, 4, 16, 41, 23, 80000, tzinfo=UTC))
    result = capture_fields(value, white_balance=(float("nan"), 0), sample_rate=0, channels=0)
    assert result[79] == "2026-10-04T16:41:23.080Z"
    assert "frame receive time in UTC" in result[100]
    assert 108 not in result
    assert 109 not in result


def test_gps_appears_in_location_with_manual_label_and_precise_camera_notes() -> None:
    from mcrawsome.library.annotations import ClipAnnotations
    from mcrawsome.resolve.metadata import editorial_fields

    value = replace(sample(), gps=(51.501, -0.12, -3.5), gps_time="2026-10-20T12:00:00Z")
    result = capture_fields(value, white_balance=None, sample_rate=0, channels=0)
    result.update(editorial_fields(ClipAnnotations(location="Stage B"), value))
    assert result[88] == "Stage B (51.501000, -0.120000)"
    assert "GPS latitude: 51.501 degrees; GPS longitude: -0.12 degrees" in result[100]
    assert "GPS altitude: -3.5 m (as recorded)" in result[100]
    assert "GPS time: 2026-10-20T12:00:00Z (as recorded)" in result[100]
    missing_height = capture_fields(
        replace(value, gps=(51.501, -0.12, None)), white_balance=None, sample_rate=0, channels=0
    )
    assert "GPS altitude" not in missing_height[100]
    assert "GPS latitude" in missing_height[100]
    invalid = capture_fields(replace(value, gps=(91, 0, 0)), white_balance=None, sample_rate=0, channels=0)
    assert "GPS" not in invalid[100]


def test_owner_shutter_example_rounds_to_24fps_180_degrees_and_one_over_48() -> None:
    value = replace(sample(), measured_fps=24.001545315493594, exposure_seconds=0.020825846)
    result = capture_fields(value, white_balance=None, sample_rate=0, channels=0)
    assert result[104] == "24"
    assert result[134] == "180"
    assert result[135] == "1/48"
    assert "0.020825846 s" in result[100]
    assert "24.0015453" in result[100]


def test_gps_only_empty_and_invalid_coordinate_locations() -> None:
    from mcrawsome.library.annotations import ClipAnnotations
    from mcrawsome.resolve.metadata import editorial_fields

    value = replace(sample(), gps=(0.0, -42.0, None))
    assert capture_fields(value, white_balance=None, sample_rate=0, channels=0)[88] == "0.000000, -42.000000"
    assert editorial_fields(ClipAnnotations(), value)[88] == "0.000000, -42.000000"
    for gps in (None, (0.0, 0.0, None), (91.0, 0.0, None), (float("nan"), 1.0, None)):
        assert editorial_fields(ClipAnnotations(location="Manual"), replace(value, gps=gps))[88] == "Manual"


def test_broadcast_rate_and_nonstandard_shutter_are_not_forced_to_distant_standards() -> None:
    value = replace(sample(), measured_fps=24000 / 1001, exposure_seconds=1 / 50)
    result = capture_fields(value, white_balance=None, sample_rate=0, channels=0)
    assert result[104] == "23.976"
    assert result[135] == "1/50"
    assert result[134] == "172.8"
    result = capture_fields(
        replace(value, exposure_seconds=1 / 53), white_balance=None, sample_rate=0, channels=0
    )
    assert result[135] == "1/53"
