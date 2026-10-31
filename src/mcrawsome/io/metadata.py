"""Typed views of the JSON metadata MotionCam stores in .mcraw files."""

from __future__ import annotations

import math
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class DngCalibration(BaseModel):
    """Still-only DNG colour data; absent on MotionCam videos, whose rendering stays unchanged."""

    model_config = ConfigDict(frozen=True)
    analog_balance: tuple[float, float, float] = (1.0, 1.0, 1.0)
    illuminants: tuple[int, int] = (17, 21)
    forward_matrices: tuple[bool, bool] = (True, True)


class ContainerMetadata(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore", populate_by_name=True)

    black_level: tuple[float, float, float, float] = Field(alias="blackLevel")
    white_level: float = Field(alias="whiteLevel")
    sensor_arrangement: Literal["rggb", "bggr", "grbg", "gbrg"] = Field(alias="sensorArrangment")
    colour_matrix_1: list[float] = Field(alias="colorMatrix1", min_length=9, max_length=9)
    colour_matrix_2: list[float] = Field(alias="colorMatrix2", min_length=9, max_length=9)
    forward_matrix_1: list[float] = Field(alias="forwardMatrix1", min_length=9, max_length=9)
    forward_matrix_2: list[float] = Field(alias="forwardMatrix2", min_length=9, max_length=9)
    calibration_matrix_1: list[float] | None = Field(
        None, alias="calibrationMatrix1", min_length=9, max_length=9
    )
    calibration_matrix_2: list[float] | None = Field(
        None, alias="calibrationMatrix2", min_length=9, max_length=9
    )
    colour_illuminant_1: str | None = Field(None, alias="colorIlluminant1")
    colour_illuminant_2: str | None = Field(None, alias="colorIlluminant2")
    dng_calibration: DngCalibration | None = None
    focal_lengths: list[float] = Field(default_factory=list, alias="focalLengths")
    apertures: list[float] = Field(default_factory=list)
    sensor_orientation: int | None = Field(None, alias="sensorOrientation")
    # Android's camera characteristics, recorded since MotionCam 5.1: 0 front, 1 back, 2 external.
    lens_facing: int | None = Field(None, alias="lensFacing")
    # fx, fy, cx, cy, skew in pixels of the pre-correction active array; k1, k2, k3, p1, p2.
    lens_intrinsic_calibration: tuple[float, ...] | None = Field(None, alias="lensIntrinsicCalibration")
    lens_distortion: tuple[float, ...] | None = Field(None, alias="lensDistortion")
    timestamp_source: int | None = Field(None, alias="timestampSource")
    use_accurate_timestamp: bool | None = None
    baseline_exposure: float | None = Field(None, alias="baselineExposure")
    exposure_compensation_step: tuple[int, int] | None = Field(None, alias="exposureCompensationStep")
    num_segments: int | None = Field(None, alias="numSegments")
    device_model: str | None = None
    camera_id: str | None = None
    manufacturer: str | None = None
    build_model: str | None = None
    build_device: str | None = None
    app_version: str | None = None
    app_release: str | None = None
    recording_type: str | None = None
    gps: tuple[float, float, float | None] | None = None  # degrees latitude/longitude, metres altitude
    gps_time: str | None = None  # MotionCam's reported GPS time; timezone is not inferred
    app_settings: dict[str, Any] = Field(default_factory=dict)  # MotionCam's own processing settings
    audio_sample_rate: int | None = None
    audio_channels: int | None = None


def gps_coordinates(
    latitude: object, longitude: object, altitude: object, *, has_time: bool = False
) -> tuple[float, float, float | None] | None:
    """Validate an optional recorded fix, excluding MotionCam's empty zero-coordinate defaults."""

    def number(value: object) -> bool:
        if not isinstance(value, int | float) or isinstance(value, bool):
            return False
        try:
            return math.isfinite(value)
        except OverflowError:
            return False

    if not number(latitude) or not number(longitude):
        return None
    assert isinstance(latitude, int | float)
    assert isinstance(longitude, int | float)
    if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
        return None
    if latitude == longitude == 0 and not has_time:
        return None
    height = float(altitude) if number(altitude) and isinstance(altitude, int | float) else None
    return float(latitude), float(longitude), height


def parse_container_metadata(raw: dict[str, Any]) -> ContainerMetadata:
    """Validate container JSON, lifting nested device and audio fields to the top level."""
    flat = dict(raw)
    profile = raw.get("deviceSpecificProfile") or {}
    flat["device_model"] = profile.get("deviceModel")
    flat["camera_id"] = profile.get("cameraId")
    extra = raw.get("extraData") or {}
    flat["use_accurate_timestamp"] = extra.get("useAccurateTimestamp")
    flat["audio_sample_rate"] = extra.get("audioSampleRate")
    flat["audio_channels"] = extra.get("audioChannels")
    flat["recording_type"] = extra.get("recordingType")
    settings = extra.get("postProcessSettings") or {}
    about = settings.get("metadata") or {}  # MotionCam stores these as flat keys with dots in the names
    flat["manufacturer"] = about.get("build.manufacturer")
    flat["build_model"] = about.get("build.model")
    flat["build_device"] = about.get("build.device")
    version = [about.get(f"version.{part}") for part in ("major", "minor", "build")]
    if all(version):
        flat["app_version"] = ".".join(str(v) for v in version)
    flat["app_release"] = about.get("app.release.type")
    gps_time = settings.get("gpsTime")
    flat["gps_time"] = gps_time.strip() if isinstance(gps_time, str) and gps_time.strip() else None
    flat["gps"] = gps_coordinates(
        settings.get("gpsLatitude"),
        settings.get("gpsLongitude"),
        settings.get("gpsAltitude"),
        has_time=bool(flat["gps_time"]),
    )
    flat["app_settings"] = {
        k: v
        for k, v in settings.items()
        if k != "metadata" and not k.startswith("gps") and not isinstance(v, list)
    }
    return ContainerMetadata.model_validate(flat)


class FrameTiming(BaseModel):
    """Capture fields for timing/geometry scans, without developing lens-shading arrays."""

    model_config = ConfigDict(frozen=True, extra="ignore", populate_by_name=True)

    width: int
    height: int
    orientation: int
    exposure_time_ns: float = Field(alias="exposureTime")
    received_ms: int = Field(alias="recvdTimestampMs")
    focus_distance: float | None = Field(None, alias="focusDistance")
    # Recorded since MotionCam 5.1: where the frame lies on the sensor and how it was read.
    original_width: int | None = Field(None, alias="originalWidth")
    original_height: int | None = Field(None, alias="originalHeight")
    is_binned: bool | None = Field(None, alias="isBinned")
    lens_intrinsic_calibration: tuple[float, ...] | None = Field(None, alias="lensIntrinsicCalibration")
    lens_distortion: tuple[float, ...] | None = Field(None, alias="lensDistortion")
    rolling_shutter_skew_ns: int | None = Field(None, alias="rollingShutterSkewNs")  # first row to last


class FrameMetadata(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore", populate_by_name=True)

    width: int
    height: int
    original_width: int = Field(alias="originalWidth")
    original_height: int = Field(alias="originalHeight")
    orientation: int
    compression_type: int = Field(alias="compressionType")
    as_shot_neutral: tuple[float, float, float] = Field(alias="asShotNeutral")
    black_level: tuple[float, float, float, float] = Field(alias="dynamicBlackLevel")
    white_level: float = Field(alias="dynamicWhiteLevel")
    iso: float
    exposure_time_ns: float = Field(alias="exposureTime")
    lens_shading_map: list[list[float]] = Field(alias="lensShadingMap")
    lens_shading_map_width: int = Field(alias="lensShadingMapWidth")
    lens_shading_map_height: int = Field(alias="lensShadingMapHeight")
    received_ms: int = Field(alias="recvdTimestampMs")
    focus_distance: float | None = Field(None, alias="focusDistance")  # dioptres (1 / metres)
    exposure_compensation: int | None = Field(None, alias="exposureCompensation")
    noise_profile: list[float] = Field(default_factory=list, alias="noiseProfile")  # (scale, offset) per site
    is_binned: bool | None = Field(None, alias="isBinned")
    # A Quad Bayer sensor read at full resolution: each colour covers 2x2 photosites (see camera/remosaic.py).
    need_remosaic: bool = Field(False, alias="needRemosaic")
    # Where the frame lies on the sensor, when it is not the centred crop MotionCam records (set by a crop).
    sensor_left: float | None = None
    sensor_top: float | None = None
    # Recorded since MotionCam 5.1 (as FrameTiming's).
    lens_intrinsic_calibration: tuple[float, ...] | None = Field(None, alias="lensIntrinsicCalibration")
    lens_distortion: tuple[float, ...] | None = Field(None, alias="lensDistortion")
    rolling_shutter_skew_ns: int | None = Field(None, alias="rollingShutterSkewNs")
