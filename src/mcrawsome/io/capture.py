"""Small, destination-neutral capture facts from the first recorded frame.

These facts describe acquisition, not the current output recipe or a whole-take variation scan.
"""

from __future__ import annotations

import math
import unicodedata
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime

from mcrawsome.io.metadata import ContainerMetadata, FrameMetadata, gps_coordinates


@dataclass(frozen=True)
class CaptureMetadata:
    manufacturer: str | None = None
    camera_model: str | None = None
    camera_id: str | None = None
    app_version: str | None = None
    app_release: str | None = None
    device_code: str | None = None
    aperture: float | None = None
    apertures: tuple[float, ...] = ()
    focal_equivalents_mm: tuple[float, ...] = ()
    iso: float | None = None
    exposure_seconds: float | None = None
    exposure_compensation_ev: float | None = None
    focus_metres: float | None = None
    focus_infinity: bool = False
    captured_utc: datetime | None = None
    measured_fps: float | None = None
    recorded_size: tuple[int, int] | None = None
    sensor_size: tuple[int, int] | None = None
    is_binned: bool | None = None
    gps: tuple[float, float, float | None] | None = None
    gps_time: str | None = None


def _text(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    if (
        not value
        or len(value) > 512
        or any(
            unicodedata.category(c) in ("Cc", "Cs")
            or 0xFDD0 <= ord(c) <= 0xFDEF
            or ord(c) & 0xFFFF in (0xFFFE, 0xFFFF)
            for c in value
        )
    ):
        return None
    return value


def _positive(value: float | None) -> float | None:
    return value if value is not None and math.isfinite(value) and value > 0 else None


def capture_snapshot(
    container: ContainerMetadata, frame: FrameMetadata, *, measured_fps: float | None
) -> CaptureMetadata:
    """Copy only validated scalar facts; never read pixels, join audio or scan other frames."""
    apertures = tuple(v for v in container.apertures if _positive(v) is not None)
    exposure = _positive(frame.exposure_time_ns)
    focus = frame.focus_distance
    compensation = None
    step = container.exposure_compensation_step
    if frame.exposure_compensation is not None and step is not None and step[1] > 0:
        try:
            ev = frame.exposure_compensation * step[0] / step[1]
            if math.isfinite(ev):
                compensation = ev
        except OverflowError:
            pass
    captured = None
    if frame.received_ms > 0:
        with suppress(OverflowError, OSError, ValueError):
            captured = datetime.fromtimestamp(frame.received_ms / 1000, UTC)
    gps_time = _text(container.gps_time)
    gps = gps_coordinates(*container.gps, has_time=gps_time is not None) if container.gps else None
    return CaptureMetadata(
        manufacturer=_text(container.manufacturer),
        camera_model=_text(container.build_model) or _text(container.device_model),
        camera_id=_text(container.camera_id),
        app_version=_text(container.app_version),
        app_release=_text(container.app_release),
        device_code=_text(container.build_device),
        aperture=apertures[0] if len(container.apertures) == 1 and apertures else None,
        apertures=apertures,
        focal_equivalents_mm=tuple(v for v in container.focal_lengths if _positive(v) is not None),
        iso=_positive(frame.iso),
        exposure_seconds=_positive(exposure / 1e9) if exposure is not None else None,
        exposure_compensation_ev=compensation,
        focus_metres=_positive(1 / focus) if _positive(focus) is not None and focus is not None else None,
        focus_infinity=focus == 0,
        captured_utc=captured,
        measured_fps=_positive(measured_fps),
        recorded_size=(frame.width, frame.height) if min(frame.width, frame.height) > 0 else None,
        sensor_size=(frame.original_width, frame.original_height)
        if min(frame.original_width, frame.original_height) > 0
        else None,
        is_binned=frame.is_binned,
        gps=gps,
        gps_time=gps_time if gps else None,
    )
