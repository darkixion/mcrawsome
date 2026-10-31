"""Metadata-only RAW defaults for native Resolve bins.

Use the Adobe DNG SDK's normalisation, rounding and NeutralToXY iteration. Native Resolve 21.1.1
exports provide independent float32 oracles. This does not change MCRAWsome's development transforms.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

import numpy as np
import numpy.typing as npt

from mcrawsome.camera.dng_colour import ILLUMINANT_TEMPERATURES, temperature
from mcrawsome.colour.robertson import uv_to_cct
from mcrawsome.colour.spaces import xy_to_xyz
from mcrawsome.output.dng import DngInfo
from mcrawsome.output.tiff import RATIONAL, SRATIONAL, entry

_D50 = (0.3457, 0.3585)  # SDK PCS white, including its published decimal precision
_ERROR = "Cannot derive Resolve bin white balance from these DNG colour tags"
Matrix = npt.NDArray[np.float64]
_RADIOMETRY_ERROR = "Unsupported Resolve bin radiometry"


@dataclass(frozen=True)
class _Field:
    number: int
    kind: int
    payload: bytes
    encoded: bytes


def _integer(data: bytes, position: int) -> tuple[int, int]:
    value = 0
    for shift in range(0, 70, 7):
        if position >= len(data):
            break
        byte = data[position]
        position += 1
        if shift == 63 and byte > 1:
            break
        value |= (byte & 127) << shift
        if byte < 128:
            return value, position
    raise ValueError(_RADIOMETRY_ERROR)


def _fields(data: bytes) -> list[_Field]:
    if not 1 <= len(data) <= 65536:
        raise ValueError(_RADIOMETRY_ERROR)
    result: list[_Field] = []
    position = 0
    while position < len(data):
        start = position
        tag, position = _integer(data, position)
        number, kind = tag >> 3, tag & 7
        if not 1 <= number < 1 << 29 or len(result) >= 256:
            raise ValueError(_RADIOMETRY_ERROR)
        payload_at = position
        if kind == 0:
            _, position = _integer(data, position)
        elif kind == 2:
            size, payload_at = _integer(data, position)
            position = payload_at + size
        elif kind in (1, 5):
            position += 8 if kind == 1 else 4
        else:
            raise ValueError(_RADIOMETRY_ERROR)
        if position > len(data):
            raise ValueError(_RADIOMETRY_ERROR)
        result.append(_Field(number, kind, data[payload_at:position], data[start:position]))
    return result


def _field(fields: list[_Field], number: int, kind: int) -> _Field:
    found = [field for field in fields if field.number == number]
    if len(found) != 1 or found[0].kind != kind:
        raise ValueError(_RADIOMETRY_ERROR)
    return found[0]


def _encoded_integer(value: int) -> bytes:
    result = bytearray()
    while value > 127:
        result.append((value & 127) | 128)
        value >>= 7
    result.append(value)
    return bytes(result)


def _updated(fields: list[_Field], replacements: dict[int, bytes]) -> bytes:
    result = []
    for field in fields:
        if field.number not in replacements:
            result.append(field.encoded)
            continue
        payload = replacements[field.number]
        result.append(_encoded_integer(field.number * 8 + field.kind))
        if field.kind == 2:
            result.append(_encoded_integer(len(payload)))
        result.append(payload)
    return b"".join(result)


def patch_white_balance(data: bytes, info: DngInfo) -> bytes:
    """Update both verified RAW defaults in an unwrapped Resolve 21.1 DNG Radiometry message.

    Unknown fields retain their original bytes. Compression belongs to the archive writer. Only
    the measured codec/layout is accepted; no global float-byte substitution is used.
    """
    fields = _fields(data)
    if _field(fields, 1, 0).payload != b"\xb8\x02" or _field(fields, 2, 0).payload != b"\x02":
        raise ValueError(_RADIOMETRY_ERROR)
    sections = {}
    settings = {}
    for number in (7, 8):
        sections[number] = _fields(_field(fields, number, 2).payload)
        if _field(sections[number], 1, 0).payload != b"\x04":
            raise ValueError(_RADIOMETRY_ERROR)
        settings[number] = _fields(_field(sections[number], 2, 2).payload)
        for target in (11, 12):
            _field(settings[number], target, 5)
    cct, tint = white_balance(info)
    values = {11: struct.pack("<f", cct), 12: struct.pack("<f", tint)}
    return _updated(
        fields,
        {number: _updated(sections[number], {2: _updated(settings[number], values)}) for number in (7, 8)},
    )


def _written(values: tuple[float, ...], size: int, *, positive: bool = False) -> Matrix:
    if len(values) != size or not all(np.isfinite(v) for v in values):
        raise ValueError(_ERROR)
    if positive and min(values) <= 0:
        raise ValueError(_ERROR)
    # Match the actual DNG rational representation before the SDK's further rounding.
    kind = RATIONAL if positive else SRATIONAL
    encoded = entry(0, kind, values)[3]
    pairs = struct.unpack(f"<{2 * size}{'I' if positive else 'i'}", encoded)
    return np.array([pairs[i] / pairs[i + 1] for i in range(0, len(pairs), 2)], dtype=np.float64)


def _round(values: Matrix, scale: int) -> Matrix:
    return np.floor(values * scale + 0.5) / scale


def _endpoint(colour: tuple[float, ...], calibration: tuple[float, ...] | None, analog: Matrix) -> Matrix:
    cm = _written(colour, 9).reshape(3, 3)
    maximum = float(np.max(cm @ xy_to_xyz(_D50)))
    if maximum <= 0:
        raise ValueError(_ERROR)
    if maximum < 0.99 or maximum > 1.01:
        cm /= maximum
    cc = _written(calibration, 9).reshape(3, 3) if calibration is not None else np.eye(3)
    # Combine each calibration endpoint before interpolating, as the SDK does.
    return np.asarray(analog @ _round(cc, 10000) @ _round(cm, 10000), dtype=np.float64)


def white_balance(info: DngInfo) -> tuple[float, float]:
    """Return native float32 (temperature Kelvin, tint), using only the tags we will write.

    No file access, RAW decoding or rendering. Missing calibration/analog tags mean identity.
    Unknown illuminants or unusable matrices are refused instead of reusing another clip's values.
    Exact native agreement is verified for the synthetic and approved MotionCam test sources.
    """
    try:
        with np.errstate(all="raise"):
            return _white_balance(info)
    except (KeyError, OverflowError, FloatingPointError, struct.error, np.linalg.LinAlgError) as exc:
        raise ValueError(_ERROR) from exc


def _white_balance(info: DngInfo) -> tuple[float, float]:
    a = ILLUMINANT_TEMPERATURES[info.illuminant_1]
    b = ILLUMINANT_TEMPERATURES[info.illuminant_2]
    neutral = _written(info.as_shot_neutral, 3, positive=True)
    neutral = _round(neutral / np.max(neutral), 1000000)
    balance = _written(info.analog_balance or (1.0, 1.0, 1.0), 3, positive=True)
    analog = np.diag(_round(balance / np.min(balance), 1000000))
    first = _endpoint(info.colour_matrix_1, info.calibration_1, analog)
    second = _endpoint(info.colour_matrix_2, info.calibration_2, analog)
    xy = _D50
    for step in range(30):
        cct = temperature(xy)
        w = float(np.clip((1 / cct - 1 / b) / (1 / a - 1 / b), 0, 1)) if a != b else 1.0
        xyz = np.linalg.solve(w * first + (1 - w) * second, neutral)
        if not np.all(np.isfinite(xyz)) or np.min(xyz) <= 0:
            raise ValueError(_ERROR)
        updated = (float(xyz[0] / xyz.sum()), float(xyz[1] / xyz.sum()))
        if sum(abs(updated[i] - xy[i]) for i in (0, 1)) < 1e-7:
            xy = updated
            break
        xy = ((updated[0] + xy[0]) * 0.5, (updated[1] + xy[1]) * 0.5) if step == 29 else updated

    x, y = xy
    denominator = -2 * x + 12 * y + 3
    cct, duv = uv_to_cct(4 * x / denominator, 6 * y / denominator)
    result = float(np.float32(cct)), float(np.float32(3000 * duv))
    if not all(np.isfinite(v) for v in result) or result[0] <= 0:
        raise ValueError(_ERROR)
    return result
