"""Descriptive metadata measured in Resolve 21.1.1 DRB and DRT exports.

The owner's empirical metadata bundle establishes the string IDs and BlobData/BA framing.
The owner confirmed Tag 0/1/2 as Untagged/Good Take/Rejected. Only video metadata is measured.
"""

from __future__ import annotations

import math
import struct
from datetime import UTC

from mcrawsome.io.capture import CaptureMetadata
from mcrawsome.io.metadata import gps_coordinates
from mcrawsome.library.annotations import ClipAnnotations
from mcrawsome.resolve.bin_codec import (
    MapField,
    WireField,
    decode_map,
    decode_wire,
    encode_map,
    encode_wire,
    patch_wire,
    unwrap,
    wrap,
)

_ERROR = "Unsupported Resolve editorial metadata"


# Snap recorder timing noise, preserving unusual intentional settings rather than inventing a standard.
_SHUTTERS = (
    1,
    2,
    3,
    4,
    5,
    6,
    8,
    10,
    12,
    15,
    20,
    24,
    25,
    30,
    40,
    48,
    50,
    60,
    80,
    96,
    100,
    120,
    125,
    160,
    192,
    200,
    240,
    250,
    320,
    400,
    480,
    500,
    640,
    800,
    960,
    1000,
    1250,
    1600,
    2000,
    2500,
    3200,
    4000,
    5000,
    6400,
    8000,
    10000,
    16000,
    32000,
)
_ANGLES = (11.25, 15, 22.5, 30, 45, 60, 72, 90, 120, 144, 172.8, 180, 216, 225, 270, 288, 324, 360)
_FRAME_RATES = (
    (24000 / 1001, "23.976"),
    (24, "24"),
    (25, "25"),
    (30000 / 1001, "29.97"),
    (30, "30"),
    (48, "48"),
    (50, "50"),
    (60000 / 1001, "59.94"),
    (60, "60"),
    (100, "100"),
    (120000 / 1001, "119.88"),
    (120, "120"),
    (240, "240"),
)


def shutter_speed(seconds: float) -> str:
    if seconds >= 1:
        return f"{seconds:.3g} s"
    denominator = 1 / seconds
    regular = min(_SHUTTERS, key=lambda value: abs(value - denominator))
    if abs(regular - denominator) / denominator <= 0.02:
        return f"1/{regular}"
    # An uncommon exposure should remain faithful: round its reciprocal, not to a distant preset.
    return f"1/{denominator:.6g}"


def shutter_angle(degrees: float) -> str:
    regular = min(_ANGLES, key=lambda value: abs(value - degrees))
    if abs(regular - degrees) <= max(0.1, degrees * 0.002):
        return f"{regular:g}"
    return f"{degrees:.2f}".rstrip("0").rstrip(".")


def camera_rate(rate: float) -> str:
    regular, label = min(_FRAME_RATES, key=lambda item: abs(item[0] - rate))
    return label if abs(regular - rate) <= 0.005 else f"{rate:.6g}"


def gps_location(capture: CaptureMetadata | None) -> str | None:
    if capture is None or capture.gps is None:
        return None
    gps = gps_coordinates(*capture.gps, has_time=bool(capture.gps_time))
    if gps is None:
        return None
    return f"{gps[0]:.6f}, {gps[1]:.6f}"


def capture_fields(
    capture: CaptureMetadata,
    *,
    white_balance: tuple[float, float] | None,
    sample_rate: int,
    channels: int,
) -> dict[int, str]:
    """Describe capture on the video item; native media descriptors remain authoritative.

    IDs are from the measured video dictionary. Separate WAV metadata blobs are not measured.
    """
    result = {
        key: value
        for key, value in ((96, capture.manufacturer), (97, capture.camera_model), (99, capture.camera_id))
        if value
    }

    def number(key: int, value: float | None, suffix: str = "", *, signed: bool = False) -> None:
        if value is not None and math.isfinite(value) and (signed or value > 0):
            result[key] = f"{value:.9g}{suffix}"

    number(107, capture.iso)
    number(120, capture.aperture)
    if 120 in result:
        result[119] = "F-stop"
    if (
        capture.exposure_seconds is not None
        and math.isfinite(capture.exposure_seconds)
        and capture.exposure_seconds > 0
    ):
        result[135] = shutter_speed(capture.exposure_seconds)
    number(138, capture.exposure_compensation_ev, " EV", signed=True)
    if capture.measured_fps is not None and math.isfinite(capture.measured_fps) and capture.measured_fps > 0:
        result[104] = camera_rate(capture.measured_fps)
    if 104 in result and 135 in result:
        assert capture.measured_fps is not None
        assert capture.exposure_seconds is not None
        degrees = 360 * capture.measured_fps * capture.exposure_seconds
        if math.isfinite(degrees):
            result[134] = shutter_angle(degrees)
    if capture.focus_infinity:
        result[122] = "Infinity"
    else:
        number(122, capture.focus_metres, " m")
    if capture.captured_utc is not None and capture.captured_utc.tzinfo is not None:
        result[79] = (
            capture.captured_utc.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        )
    if white_balance is not None and all(math.isfinite(v) for v in white_balance) and white_balance[0] > 0:
        number(108, white_balance[0])
        number(109, white_balance[1], signed=True)
    lens = []
    if capture.focal_equivalents_mm:
        lens.append(
            "Reported focal length: "
            + ", ".join(f"{v:.9g}" for v in capture.focal_equivalents_mm)
            + " mm (35 mm equivalent)"
        )
    if capture.aperture is None and capture.apertures:
        lens.append(
            "Reported f-numbers: "
            + ", ".join(f"{v:.9g}" for v in capture.apertures)
            + "; active aperture unknown"
        )
    if lens:
        result[118] = "; ".join(lens)
    if capture.recorded_size is not None:
        result[129] = f"{capture.recorded_size[0]} x {capture.recorded_size[1]} px"
        if capture.sensor_size is not None:
            result[129] += f" within {capture.sensor_size[0]} x {capture.sensor_size[1]} px"
        if capture.is_binned is not None:
            result[129] += "; binned" if capture.is_binned else "; not binned"
    notes = []
    if result:
        notes.append("Capture values describe the first recorded frame; variation is not scanned")
    if 79 in result:
        notes.append("Date Recorded is the frame receive time in UTC")
    if 104 in result:
        notes.append(f"Measured source FPS: {capture.measured_fps:.9g}; independent of output rate")
    if 134 in result:
        notes.append(
            "Shutter Angle is computed from exposure and source FPS; nearby standard values are rounded"
        )
    if 135 in result:
        notes.append(f"Recorded exposure: {capture.exposure_seconds:.12g} s; displayed as a nominal fraction")
    if 122 in result:
        notes.append("Distance is estimated from the recorded focus setting")
    if 108 in result:
        notes.append("White balance is derived from the emitted DNG calibration and neutral")
    if capture.app_version:
        release = f" {capture.app_release.title()}" if capture.app_release else ""
        notes.append(f"Recorded with MotionCam{release} {capture.app_version}")
    if capture.device_code:
        notes.append(f"Android device code: {capture.device_code}")
    gps = gps_coordinates(*capture.gps, has_time=bool(capture.gps_time)) if capture.gps else None
    if gps:
        result[88] = gps_location(capture) or ""
        latitude, longitude, altitude = gps
        notes.append(f"GPS latitude: {latitude:.9g} degrees; GPS longitude: {longitude:.9g} degrees")
        if altitude is not None:
            notes.append(f"GPS altitude: {altitude:.9g} m (as recorded)")
        if capture.gps_time:
            notes.append(f"GPS time: {capture.gps_time} (as recorded)")
    if notes:
        result[100] = "; ".join(notes)
    if sample_rate > 0 and channels > 0:
        result[220] = f"{sample_rate / 1000:.9g}"
        result[213] = "WAV"
        result[211] = f"{channels} channel{'s' if channels != 1 else ''}; 16-bit PCM; separate aligned WAV"
    return result


def keywords_field(own: list[str], measured: tuple[str, ...] = ()) -> str | None:
    """The user's keywords, then measured ones they do not already have (case ignored)."""
    words = list(own)
    words += [word for word in measured if word.lower() not in {item.lower() for item in words}]
    if any(not word or word != word.strip() or any(c in word for c in ',"\n\t') for word in words):
        raise ValueError("Resolve keywords must be individual terms without commas, quotes or line breaks")
    return ",".join(words) if words else None


def editorial_fields(
    details: ClipAnnotations, capture: CaptureMetadata | None = None, measured: tuple[str, ...] = ()
) -> dict[int, str]:
    strings = {
        64: details.description,
        65: details.notes,
        66: details.scene,
        67: details.shot,
        68: details.take,
        80: details.camera,
        88: details.location,
    }
    result = {number: text for number, text in strings.items() if text}
    if coordinates := gps_location(capture):
        result[88] = f"{details.location} ({coordinates})" if details.location else coordinates
    result[77] = {"untagged": "0", "good-take": "1", "rejected": "2"}[details.tag]
    if keywords := keywords_field(details.keywords, measured):
        result[74] = keywords
    return result


def patch_metadata(data: bytes, updates: dict[int, str]) -> bytes:
    """Keep unknown records and flags; refuse unfamiliar framing rather than discard it."""
    if not updates:
        return data
    if data:
        fields = decode_map(data)
        item = fields.get("BlobData")
        if item is None or item.kind != 12 or item.nullable != 0 or item.value is None:
            raise ValueError(_ERROR)
        message, mode = unwrap(item.value)
        if mode not in ((10001, 128), (10001, 129)):
            raise ValueError(_ERROR)
        outer = decode_wire(message)
        sections = [f for f in outer if f.number == 2]
        if len(sections) != 1 or sections[0].kind != 2 or not isinstance(sections[0].value, bytes):
            raise ValueError(_ERROR)
        body = sections[0].value
        if len(body) < 8 or struct.unpack(">II", body[:8]) != (1000001, len(body) - 8):
            raise ValueError(_ERROR)
        records = decode_wire(body[8:])
    else:
        fields = {}
        mode = (10001, 129)
        message = encode_wire([WireField(2, 2, b"")])
        records = []
    seen = set()
    changed = []
    for record in records:
        if record.number == 2 and record.kind == 2 and isinstance(record.value, bytes):
            inner = decode_wire(record.value)
            identifiers = [f for f in inner if f.number == 1]
            if len(identifiers) != 1 or identifiers[0].kind != 0 or not isinstance(identifiers[0].value, int):
                raise ValueError(_ERROR)
            number = identifiers[0].value
            if number in updates:
                if number in seen or not any(f.number == 2 and f.kind == 2 for f in inner):
                    raise ValueError(_ERROR)
                seen.add(number)
                record = WireField(2, 2, patch_wire(record.value, {2: updates[number].encode("utf-8")}))
        changed.append(record)
    for number, text in updates.items():
        if number not in seen:
            changed.append(
                WireField(2, 2, encode_wire([WireField(1, 0, number), WireField(2, 2, text.encode("utf-8"))]))
            )
    payload = encode_wire(changed)
    body = struct.pack(">II", 1000001, len(payload)) + payload
    fields["BlobData"] = MapField(12, 0, wrap(patch_wire(message, {2: body}), mode))
    return encode_map(fields)
