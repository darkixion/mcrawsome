"""A small CinemaDNG writer: full or half-size CFA frames with the camera's colour data.

Lens shading travels as standard GainMap opcodes (OpcodeList2), which raw developers apply after black
subtraction. Full-size frames keep the sensor values; proxies average four samples per Bayer site.
"""

from __future__ import annotations

import math
import struct
import threading
from collections.abc import Sequence
from dataclasses import dataclass, replace
from fractions import Fraction
from pathlib import Path
from typing import Any, Literal

import numpy as np
import numpy.typing as npt

from mcrawsome import __version__, _raw
from mcrawsome._raw import bake_cfa, encode_ljpeg
from mcrawsome.camera.shading import ShadingMode, lens_map, sensor_offset, shading_gains
from mcrawsome.develop.raw import Levels, levels
from mcrawsome.io.metadata import ContainerMetadata, FrameMetadata
from mcrawsome.io.orientation import dng_orientation
from mcrawsome.output import dng_log
from mcrawsome.output.tiff import entry as _entry
from mcrawsome.output.tiff import tiff_header, write_tiff

_BYTE, _ASCII, _SHORT, _LONG, _RATIONAL, _UNDEFINED, _SRATIONAL, _FLOAT, _DOUBLE = (
    1,
    2,
    3,
    4,
    5,
    7,
    10,
    11,
    12,
)
_SIZES = {_BYTE: 1, _ASCII: 1, _SHORT: 2, _LONG: 4, _RATIONAL: 8, _UNDEFINED: 1, _SRATIONAL: 8, _FLOAT: 4,
          _DOUBLE: 8}  # fmt: skip
_CFA_CODES = {"r": 0, "g": 1, "b": 2}
_SITES = ((0, 0), (0, 1), (1, 0), (1, 1))
# Android's names for the matrices' reference illuminants, as EXIF LightSource codes.
_ILLUMINANTS = {
    "daylight": 1, "fluorescent": 2, "tungsten": 3, "flash": 4, "fine_weather": 9, "cloudy_weather": 10,
    "shade": 11, "daylight_fluorescent": 12, "day_white_fluorescent": 13, "cool_white_fluorescent": 14,
    "white_fluorescent": 15, "standarda": 17, "standardb": 18, "standardc": 19, "d55": 20, "d65": 21,
    "d75": 22, "d50": 23, "iso_studio_tungsten": 24,
}  # fmt: skip
_ILLUMINANT_A, _ILLUMINANT_D65 = 17, 21  # assumed when a file does not say
_GAIN_MAP = 9


@dataclass(frozen=True)
class DngInfo:
    width: int
    height: int
    cfa: str
    black_levels: tuple[float, float, float, float]
    white_level: int
    colour_matrix_1: tuple[float, ...]
    colour_matrix_2: tuple[float, ...]
    forward_matrix_1: tuple[float, ...] | None  # None leaves readers to use the colour matrices
    forward_matrix_2: tuple[float, ...] | None
    calibration_1: tuple[float, ...] | None
    calibration_2: tuple[float, ...] | None
    illuminant_1: int
    illuminant_2: int
    noise_profile: tuple[float, ...] | None  # (scale, offset) pairs for R, G, B
    as_shot_neutral: tuple[float, float, float]
    orientation: int  # TIFF value: 1 normal, 6 clockwise 90, 3 rotated 180, 8 clockwise 270
    make: str
    model: str
    frame_rate: Fraction | None  # None for a still: no FrameRate tag
    baseline_exposure: float
    gain_maps: bytes
    unique_model: str = ""  # UniqueCameraModel; empty means "<make> <model>"
    default_crop: tuple[int, int, int, int] | None = None  # left, top, width, height
    opcode_list_3: bytes | None = None  # carried through from a still
    analog_balance: tuple[float, float, float] | None = None
    timecodes: bytes | None = None  # CinemaDNG TimeCodes: the frame's 8-byte SMPTE time address


def dng_info(
    container: ContainerMetadata,
    frame: FrameMetadata,
    frame_rate: Fraction,
    exposure_stops: float,
    lens_shading: float = 1.0,
    *,
    shading_mode: ShadingMode = "full",
    black_level: float | Levels | None = None,
    white_level: float | None = None,
    baked_bits: int | None = None,
    rotation: int = 0,
) -> DngInfo:
    """`black_level` and `white_level` replace the frame's levels when set. With `baked_bits` the lens
    shading is in the raw values (see bake_shading): no gain maps, and the exposure is raised by the bits
    the values were scaled by."""
    model = container.device_model or "MotionCam"
    blacks, white = levels(frame, black_level, white_level)
    return DngInfo(
        analog_balance=container.dng_calibration.analog_balance if container.dng_calibration else None,
        width=frame.width,
        height=frame.height,
        cfa=container.sensor_arrangement,
        black_levels=blacks,
        white_level=round(white),
        colour_matrix_1=tuple(container.colour_matrix_1),
        colour_matrix_2=tuple(container.colour_matrix_2),
        forward_matrix_1=(
            tuple(container.forward_matrix_1)
            if container.dng_calibration is None or container.dng_calibration.forward_matrices[0]
            else None
        ),
        forward_matrix_2=(
            tuple(container.forward_matrix_2)
            if container.dng_calibration is None or container.dng_calibration.forward_matrices[1]
            else None
        ),
        calibration_1=tuple(container.calibration_matrix_1[:9]) if container.calibration_matrix_1 else None,
        calibration_2=tuple(container.calibration_matrix_2[:9]) if container.calibration_matrix_2 else None,
        illuminant_1=(
            container.dng_calibration.illuminants[0]
            if container.dng_calibration
            else _ILLUMINANTS.get(container.colour_illuminant_1 or "", _ILLUMINANT_A)
        ),
        illuminant_2=(
            container.dng_calibration.illuminants[1]
            if container.dng_calibration
            else _ILLUMINANTS.get(container.colour_illuminant_2 or "", _ILLUMINANT_D65)
        ),
        noise_profile=noise_profile(frame.noise_profile),
        as_shot_neutral=frame.as_shot_neutral,
        orientation=dng_orientation(frame.orientation, rotation),
        make="MotionCam",
        model=model,
        frame_rate=frame_rate,
        baseline_exposure=exposure_stops + (baked_bits or 0),
        gain_maps=(
            b""
            if baked_bits is not None
            else gain_map_opcodes(frame, container.sensor_arrangement, lens_shading, shading_mode)
        ),
    )


# Camera models whose names give better defaults in Resolve's RAW tab (metadata only): Make and
# Model; UniqueCameraModel is the preset's name.
CAMERA_PRESETS = {
    "Panasonic Varicam RAW": ("Panasonic", "Varicam RAW"),
    "Blackmagic Pocket Cinema Camera 4K": ("Blackmagic Design", "Pocket Cinema Camera 4K"),
}


def camera_model(device: str, clip_stem: str, override: str | None, unique: bool) -> tuple[str, str, str]:
    """(Make, Model, UniqueCameraModel). `override` names another camera (a preset, or any text); `unique`
    adds the clip's name, so Resolve keeps each clip's RAW settings apart."""
    suffix = f" {clip_stem}" if unique else ""
    if override is None:
        return "MotionCam", device + suffix, f"MotionCam {device}{suffix}"
    if override in CAMERA_PRESETS:
        make, model = CAMERA_PRESETS[override]
        return make, model + suffix, override + suffix
    return "MotionCam", override + suffix, override + suffix


def with_camera_model(info: DngInfo, names: tuple[str, str, str]) -> DngInfo:
    """Apply camera identity and the Panasonic preset's decoder exposure compensation.

    MotionCam's Panasonic CinemaDNG compatibility preset uses a -2 EV BaselineExposure
    offset. Keep it additive to normalisation/baking headroom and leave sensor samples and
    colour calibration unchanged. Clip-name suffixes do not change the preset's exposure.
    """
    make, model, unique = names
    panasonic = make == "Panasonic" and (model == "Varicam RAW" or model.startswith("Varicam RAW "))
    return replace(
        info,
        make=make,
        model=model,
        unique_model=unique,
        baseline_exposure=info.baseline_exposure - (2.0 if panasonic else 0.0),
    )


def with_matrix(info: DngInfo, matrix: tuple[float, ...]) -> DngInfo:
    """A colour matrix override: both ColorMatrix tags, and no ForwardMatrix (readers derive it)."""
    return replace(
        info, colour_matrix_1=matrix, colour_matrix_2=matrix, forward_matrix_1=None, forward_matrix_2=None
    )


def noise_profile(android: list[float]) -> tuple[float, ...] | None:
    """Android gives (scale, offset) for R, G even, G odd and B; DNG wants one pair per colour plane."""
    if len(android) == 8:
        r, g1, g2, b = (android[i : i + 2] for i in range(0, 8, 2))
        return (r[0], r[1], (g1[0] + g2[0]) / 2, (g1[1] + g2[1]) / 2, b[0], b[1])
    if len(android) in (2, 6):
        return tuple(android)
    return None


# --- gain maps ----------------------------------------------------------------------------------------


@dataclass(frozen=True)
class GainMap:
    top: int
    left: int
    points_v: int
    points_h: int
    spacing_v: float
    spacing_h: float
    origin_v: float
    origin_h: float
    gains: npt.NDArray[np.float32]  # (points_v, points_h)

    def evaluate(self, ys: npt.NDArray[np.float64], xs: npt.NDArray[np.float64]) -> npt.NDArray[np.float64]:
        """Bilinear gains at relative image positions (0 to 1), clamped at the map's edges."""
        fy = np.clip((ys - self.origin_v) / self.spacing_v, 0, self.points_v - 1)
        fx = np.clip((xs - self.origin_h) / self.spacing_h, 0, self.points_h - 1)
        y0 = np.minimum(np.floor(fy).astype(int), self.points_v - 2)
        x0 = np.minimum(np.floor(fx).astype(int), self.points_h - 2)
        wy, wx = (fy - y0)[:, None], (fx - x0)[None, :]
        g = self.gains.astype(np.float64)
        top = g[y0][:, x0] * (1 - wx) + g[y0][:, x0 + 1] * wx
        bottom = g[y0 + 1][:, x0] * (1 - wx) + g[y0 + 1][:, x0 + 1] * wx
        return np.asarray(top * (1 - wy) + bottom * wy, dtype=np.float64)


def gain_map_opcodes(
    frame: FrameMetadata, cfa: str, strength: float = 1.0, mode: ShadingMode = "full"
) -> bytes:
    """Four GainMap opcodes (one per Bayer site) from MotionCam's full-sensor shading map, raised to
    `strength` (1 corrects the vignette fully, 0 keeps it); `mode` "colour" keeps only the colour shading.

    The map spans the whole sensor and the frame lies on it as in camera/shading.py.
    """
    lw, lh = frame.lens_shading_map_width, frame.lens_shading_map_height
    planes = lens_map(frame, mode) ** np.float32(strength)
    ox, oy = sensor_offset(frame)
    spacing_h = frame.original_width / ((lw - 1) * frame.width)
    spacing_v = frame.original_height / ((lh - 1) * frame.height)
    r_row = _SITES[cfa.index("r")][0]
    opcodes = []
    for i, (y, x) in enumerate(_SITES):
        colour = cfa[i]
        plane = 0 if colour == "r" else 3 if colour == "b" else (1 if y == r_row else 2)
        params = struct.pack(
            ">8I2I4dI", y, x, frame.height, frame.width, 0, 1, 2, 2, lh, lw,
            spacing_v, spacing_h, -oy / frame.height, -ox / frame.width, 1,
        ) + planes[plane].astype(">f4").tobytes()  # fmt: skip
        opcodes.append(struct.pack(">4I", _GAIN_MAP, 0x01030000, 0, len(params)) + params)
    return struct.pack(">I", len(opcodes)) + b"".join(opcodes)


def gain_map_planes(
    planes: tuple[npt.NDArray[np.float32], ...],
    width: int,
    height: int,
    cfa: str,
    points: tuple[int, int] = (32, 32),
) -> bytes:
    """Four GainMap opcodes from half-size gain planes (R, G on R rows, G on B rows, B), each sampled on a
    grid of `points` (rows, columns) from its site's first to its last photosite."""
    from scipy.ndimage import map_coordinates

    pv, ph = points
    rows, cols = planes[0].shape
    r_row = _SITES[cfa.index("r")][0]
    opcodes = []
    for i, (y, x) in enumerate(_SITES):
        colour = cfa[i]
        plane = planes[0 if colour == "r" else 3 if colour == "b" else (1 if y == r_row else 2)]
        # The map spans this site's own first and last photosites, in relative image positions.
        origin_v, origin_h = (y + 0.5) / height, (x + 0.5) / width
        spacing_v, spacing_h = 2 * (rows - 1) / height / (pv - 1), 2 * (cols - 1) / width / (ph - 1)
        grid = np.meshgrid(np.linspace(0, rows - 1, pv), np.linspace(0, cols - 1, ph), indexing="ij")
        gains = map_coordinates(plane.astype(np.float64), grid, order=1, mode="nearest")
        area = (y, x, height, width, 0, 1, 2, 2, pv, ph)
        params = struct.pack(">8I2I4dI", *area, spacing_v, spacing_h, origin_v, origin_h, 1)
        params += np.asarray(gains, ">f4").tobytes()
        opcodes.append(struct.pack(">4I", _GAIN_MAP, 0x01030000, 0, len(params)) + params)
    return struct.pack(">I", len(opcodes)) + b"".join(opcodes)


def parse_gain_maps(blob: bytes) -> list[GainMap]:
    (count,) = struct.unpack_from(">I", blob, 0)
    pos, maps = 4, []
    for _ in range(count):
        opcode, _, _, size = struct.unpack_from(">4I", blob, pos)
        pos += 16
        if opcode == _GAIN_MAP:
            top, left, _, _, _, _, _, _, pv, ph = struct.unpack_from(">10I", blob, pos)
            sv, sh, ov, oh = struct.unpack_from(">4d", blob, pos + 40)
            gains = np.frombuffer(blob, ">f4", pv * ph, pos + 76).astype(np.float32).reshape(pv, ph)
            maps.append(GainMap(top, left, pv, ph, sv, sh, ov, oh, gains))
        pos += size
    return maps


def shading_scale_bits(frame: FrameMetadata, strength: float, mode: ShadingMode) -> int:
    """Bits the raw values grow by when this frame's lens shading is baked in: the largest gain, rounded up
    to a power of two."""
    largest = float(lens_map(frame, mode).max()) ** strength
    return max(0, math.ceil(math.log2(largest) - 1e-9))


def bake_shading(
    cfa: npt.NDArray[np.uint16],
    frame: FrameMetadata,
    pattern: str,
    strength: float,
    mode: ShadingMode,
    blacks: Levels,
    white: float,
    bits: int,
) -> tuple[npt.NDArray[np.uint16], int]:
    """The raw values with the lens shading applied (as a raw developer applies the gain maps: after black
    subtraction), and the white level they are stored with. `bits` > 0 scales the range by 2^bits (Scale
    Raw): the white level rises by as much, so the correction cannot clip highlights; with 0, values clip
    at the sensor's white level."""
    gains = shading_gains(frame, mode)
    new_white = baked_white(blacks, white, bits)
    if new_white > 65535:
        raise ValueError("baked shading exceeds the 16-bit raw range; lower the white level or lens shading")
    r_row = _SITES[pattern.index("r")][0]
    out = np.empty(cfa.shape, np.uint16)
    for i, (y, x) in enumerate(_SITES):
        colour = pattern[i]
        plane = gains[0] if colour == "r" else gains[3] if colour == "b" else gains[1 if y == r_row else 2]
        site = cfa[y::2, x::2].astype(np.float32)
        black = np.float32(blacks[i])
        h, w = site.shape
        value = black + (site - black) * plane[:h, :w] ** np.float32(strength)
        out[y::2, x::2] = np.clip(np.rint(value), 0, new_white).astype(np.uint16)
    return out, new_white


def baked_white(blacks: Levels, white: float, bits: int) -> int:
    """The white level of raw values baked with `bits` of headroom (see bake_shading)."""
    top = float(max(blacks))
    return round(top + (white - top) * (1 << bits))


def mount_bake_bits(
    frame: FrameMetadata, strength: float, mode: ShadingMode, blacks: Levels, white: float
) -> int:
    """The bits a mounted sequence's raw values grow by when its lens shading is baked, without reading the
    whole clip: 2 for a full correction (12-bit values become 14-bit; every clip
    measured needed at most 4x), more if this first frame needs it, none for a colour-only correction, and
    never more than 16 bits can hold."""
    needed = shading_scale_bits(frame, strength, mode)
    bits = needed if mode == "colour" else max(2, needed)
    while bits and baked_white(blacks, white, bits) > 65535:
        bits -= 1
    return bits


def exposure_gain(reference: FrameMetadata, frame: FrameMetadata) -> float:
    """How much `frame`'s raw values are scaled to have `reference`'s exposure (ISO times shutter): the
    recorded exposure changes of auto exposure or a timelapse ramp, from metadata alone. 1 when either
    exposure is unknown."""
    wanted, recorded = reference.iso * reference.exposure_time_ns, frame.iso * frame.exposure_time_ns
    return float(wanted / recorded) if wanted > 0 and recorded > 0 else 1.0


def exposure_headroom(gains: Sequence[float]) -> int:
    """The bits raw values are given so per-frame exposure gains measured in advance do not clip: the
    largest brightening rounded up to a power of two, at most 2 (a frame brightened further clips at the
    raised white level). Darkened frames need none."""
    largest = max(gains, default=1.0)
    return min(2, max(0, math.ceil(math.log2(largest) - 1e-9))) if largest > 1 else 0


class ShadingBaker:
    """bake_shading for a mounted sequence, fast enough to play back: the frame-sized gains are made once
    for each distinct shading map and kept, and each frame is one native pass (mcrawsome._raw.bake_cfa).
    The values are bake_shading's, except that a frame whose levels would need more than 16 bits clips at
    65535 instead of being refused. Without `shading` only an exposure `gain` is applied. Safe to share
    between the demand and read-ahead threads."""

    def __init__(
        self, pattern: str, strength: float, mode: ShadingMode, bits: int, *, shading: bool = True
    ) -> None:
        self.pattern, self.strength, self.mode, self.bits = pattern, strength, mode, bits
        self.shading = shading
        self._lock = threading.Lock()
        self._key: tuple[object, ...] | None = None
        self._gains: npt.NDArray[np.float32] | None = None

    def white(self, blacks: Levels, white: float) -> int:
        return min(65535, baked_white(blacks, white, self.bits))

    def bake(
        self,
        cfa: npt.NDArray[np.uint16],
        frame: FrameMetadata,
        blacks: Levels,
        white: float,
        gain: float = 1.0,
    ) -> npt.NDArray[np.uint16]:
        black = np.array([[blacks[0], blacks[1]], [blacks[2], blacks[3]]], np.float32)  # _SITES order
        gains = self._mosaic(frame, cfa.shape) if self.shading else None
        return bake_cfa(np.ascontiguousarray(cfa), black, gains, gain, self.white(blacks, white))

    def _mosaic(self, frame: FrameMetadata, shape: tuple[int, ...]) -> npt.NDArray[np.float32]:
        key = (
            shape, frame.width, frame.height, frame.original_width, frame.original_height,
            sensor_offset(frame), frame.lens_shading_map_width, frame.lens_shading_map_height,
            np.asarray(frame.lens_shading_map, np.float32).tobytes(),
        )  # fmt: skip
        with self._lock:
            if key != self._key or self._gains is None:
                planes = shading_gains(frame, self.mode)
                r_row = _SITES[self.pattern.index("r")][0]
                h, w = shape
                gains = np.empty(shape, np.float32)
                for i, (y, x) in enumerate(_SITES):
                    colour = self.pattern[i]
                    plane = (
                        planes[0]
                        if colour == "r"
                        else planes[3]
                        if colour == "b"
                        else planes[1 if y == r_row else 2]
                    )
                    gains[y::2, x::2] = plane[: h // 2, : w // 2] ** np.float32(self.strength)
                self._key, self._gains = key, gains
            return self._gains


# --- TIFF writing and reading --------------------------------------------------------------------------


TILE = 256


DngScale = Literal["full", "half", "quarter", "eighth"]
SCALE_FACTORS: dict[str, int] = {"full": 1, "half": 2, "quarter": 4, "eighth": 8}


def reduce_cfa(cfa: npt.NDArray[np.uint16], pattern: str, factor: int) -> npt.NDArray[np.uint16]:
    """Average `factor` x `factor` samples of each Bayer site into the same site of a CFA reduced by
    `factor` in each direction.

    Incomplete blocks (2 x `factor` samples square) at the bottom and right are cropped; the exporter notes
    this crop. The two green sites stay separate, keeping their black levels and the Bayer pattern.
    """
    if pattern not in ("rggb", "grbg", "gbrg", "bggr"):
        raise ValueError(f"unsupported Bayer pattern: {pattern}")
    block = 2 * factor
    height, width = (n // block * block for n in cfa.shape)
    if min(height, width) == 0:
        raise ValueError(f"a DNG reduced {factor} times needs at least a {block}x{block} CFA")
    if factor in (2, 4, 8):
        return _raw.reduce_cfa(cfa, factor)
    result = np.empty((height // factor, width // factor), dtype=np.uint16)
    for y, x in _SITES:
        total = np.zeros((height // block, width // block), np.uint32)
        for dy in range(0, block, 2):
            for dx in range(0, block, 2):
                total += cfa[y + dy : height : block, x + dx : width : block]
        result[y::2, x::2] = np.rint(total / factor**2).astype(np.uint16)
    return result


def half_cfa(cfa: npt.NDArray[np.uint16], pattern: str) -> npt.NDArray[np.uint16]:
    """Average four samples of each Bayer site into the same site of a half-size CFA (see reduce_cfa)."""
    return reduce_cfa(cfa, pattern, 2)


def reduced_info(info: DngInfo, width: int, height: int, factor: int) -> DngInfo:
    """Scale opcode areas and noise variance; gain-map positions are normalised to the cropped image."""
    blob = bytearray(info.gain_maps)
    (count,) = struct.unpack_from(">I", blob, 0) if blob else (0,)
    pos = 4
    for _ in range(count):
        opcode, _, _, size = struct.unpack_from(">4I", blob, pos)
        pos += 16
        if opcode == _GAIN_MAP:
            # Keep the Bayer site's top/left and pitch: the reduced image has the same pattern.
            struct.pack_into(">2I", blob, pos + 8, height, width)
            sv, sh, ov, oh = struct.unpack_from(">4d", blob, pos + 40)
            ry, rx = info.height / (height * factor), info.width / (width * factor)
            struct.pack_into(">4d", blob, pos + 40, sv * ry, sh * rx, ov * ry, oh * rx)
        pos += size
    noise = tuple(v / factor**2 for v in info.noise_profile) if info.noise_profile else None
    return replace(info, width=width, height=height, gain_maps=bytes(blob), noise_profile=noise)


def _tiles(cfa_data: npt.NDArray[np.uint16], bits: int) -> list[bytes]:
    """Lossless JPEG tiles, row by row; edge tiles are padded by repeating the last row and column."""
    height, width = cfa_data.shape
    blocks = []
    for top in range(0, height, TILE):
        for left in range(0, width, TILE):
            tile = cfa_data[top : top + TILE, left : left + TILE]
            pad = ((0, TILE - tile.shape[0]), (0, TILE - tile.shape[1]))
            if pad[0][1] or pad[1][1]:
                tile = np.pad(tile, pad, mode="edge")
            blocks.append(encode_ljpeg(np.ascontiguousarray(tile), bits))
    return blocks


def write_dng(
    path: Path,
    cfa_data: npt.NDArray[np.uint16],
    info: DngInfo,
    compression: Literal["none", "lossless"] = "none",
    *,
    scale: DngScale = "full",
    log_bits: int | None = None,
    seed: int = 0,
) -> None:
    """One uncompressed strip, or (lossless) 256x256 lossless JPEG tiles (DNG compression 7). With
    `log_bits`, the values are stored as that many bits of log codes (dithered with `seed`) with a
    LinearizationTable; uncompressed codes of 8 bits or fewer take one byte each."""
    factor = SCALE_FACTORS[scale]
    if factor > 1:
        cfa_data = reduce_cfa(cfa_data, info.cfa, factor)
        info = reduced_info(info, cfa_data.shape[1], cfa_data.shape[0], factor)
    height, width = cfa_data.shape
    linearisation: list[tuple[int, int, int, bytes]] = []
    if log_bits is not None:
        cfa_data = dng_log.encode(cfa_data, info.white_level, log_bits, seed)
        linearisation = [_entry(50712, _SHORT, list(dng_log.table(info.white_level, log_bits)))]
    if compression == "lossless":
        bits = (
            log_bits
            if log_bits is not None
            else max(2, int(info.white_level).bit_length(), int(cfa_data.max()).bit_length())
        )
        blocks = _tiles(np.asarray(cfa_data, dtype=np.uint16), bits)
        image = [
            _entry(258, _SHORT, [bits]),
            _entry(259, _SHORT, [7]),
            _entry(322, _LONG, [TILE]),
            _entry(323, _LONG, [TILE]),
            _entry(324, _LONG, [0] * len(blocks)),  # tile offsets, filled in below
            _entry(325, _LONG, [len(b) for b in blocks]),
        ]
    else:
        sample = "u1" if log_bits is not None and log_bits <= 8 else "<u2"
        blocks = [np.ascontiguousarray(cfa_data, dtype=sample).tobytes()]
        image = [
            _entry(258, _SHORT, [8 if sample == "u1" else 16]),
            _entry(259, _SHORT, [1]),
            _entry(273, _LONG, [0]),  # strip offset, filled in below
            _entry(278, _LONG, [height]),
            _entry(279, _LONG, [len(blocks[0])]),
        ]
    info = replace(info, width=width, height=height)
    entries = [*image, *linearisation, *_mosaic_entries(info)]
    if factor > 1:
        entries += _reduced_entries(width, height)
    elif info.default_crop is not None:
        entries += _crop_entries(info.default_crop)
    write_tiff(path, entries, blocks)


def _mosaic_entries(info: DngInfo) -> list[tuple[int, int, int, bytes]]:
    entries = [
        *_camera_entries(info),
        _entry(256, _LONG, [info.width]),
        _entry(257, _LONG, [info.height]),
        _entry(262, _SHORT, [32803]),  # colour filter array
        _entry(277, _SHORT, [1]),
        _entry(284, _SHORT, [1]),
        _entry(33421, _SHORT, [2, 2]),
        _entry(33422, _BYTE, [_CFA_CODES[c] for c in info.cfa]),
        _entry(50710, _BYTE, [0, 1, 2]),
        _entry(50711, _SHORT, [1]),
        _entry(50713, _SHORT, [2, 2]),
        _entry(50714, _RATIONAL, info.black_levels),
        _entry(50717, _LONG, [info.white_level]),
    ]
    if info.gain_maps:  # none when the lens shading is baked into the values
        entries.append(_entry(51009, _UNDEFINED, info.gain_maps))
    if info.noise_profile:
        data = struct.pack(f"<{len(info.noise_profile)}d", *info.noise_profile)
        entries.append((51041, _DOUBLE, len(info.noise_profile), data))
    return entries


def _reduced_entries(width: int, height: int) -> list[tuple[int, int, int, bytes]]:
    """A proxy's default crop and active area: all of its reduced image."""
    return [
        _entry(50719, _LONG, [0, 0]),
        _entry(50720, _LONG, [width, height]),
        _entry(50829, _LONG, [0, 0, height, width]),
    ]


def uncompressed_header(info: DngInfo, *, reduced: bool = False, log_bits: int | None = None) -> bytes:
    """A 16-bit sensor DNG header, without reading or allocating its pixels. `reduced`: a proxy (see
    reduced_info), described as write_dng describes one. `log_bits`: log codes with their
    LinearizationTable, one byte each up to 8 bits, as write_dng stores them."""
    sample = 1 if log_bits is not None and log_bits <= 8 else 2
    count = info.width * info.height * sample
    linearisation = (
        [_entry(50712, _SHORT, list(dng_log.table(info.white_level, log_bits)))]
        if log_bits is not None
        else []
    )
    entries = [
        *_mosaic_entries(info),
        *linearisation,
        _entry(258, _SHORT, [8 * sample]),
        _entry(259, _SHORT, [1]),
        _entry(273, _LONG, [0]),
        _entry(278, _LONG, [info.height]),
        _entry(279, _LONG, [count]),
    ]
    if reduced:
        entries += _reduced_entries(info.width, info.height)
    elif info.default_crop is not None:
        entries += _crop_entries(info.default_crop)
    # Keep ordinary buffered metadata reads on metadata-only pages in a virtual file.
    return tiff_header(entries, [count], alignment=131072, padding=131072)


def _camera_entries(info: DngInfo) -> list[tuple[int, int, int, bytes]]:
    """The tags a mosaic and a linear DNG share: camera, orientation, colour and exposure."""
    entries = [
        _entry(254, _LONG, [0]),
        _entry(271, _ASCII, info.make),
        _entry(272, _ASCII, info.model),
        _entry(274, _SHORT, [info.orientation]),
        _entry(305, _ASCII, f"MCRAWsome {__version__}"),
        _entry(50706, _BYTE, [1, 4, 0, 0]),
        _entry(50707, _BYTE, [1, 3, 0, 0]),
        _entry(50708, _ASCII, info.unique_model or f"{info.make} {info.model}"),
        _entry(50721, _SRATIONAL, info.colour_matrix_1),
        _entry(50722, _SRATIONAL, info.colour_matrix_2),
        _entry(50728, _RATIONAL, info.as_shot_neutral),
        _entry(50730, _SRATIONAL, (info.baseline_exposure,)),
        _entry(50778, _SHORT, [info.illuminant_1]),
        _entry(50779, _SHORT, [info.illuminant_2]),
    ]
    if info.forward_matrix_1 is not None:
        entries.append(_entry(50964, _SRATIONAL, info.forward_matrix_1))
    if info.forward_matrix_2 is not None:
        entries.append(_entry(50965, _SRATIONAL, info.forward_matrix_2))
    if info.frame_rate is not None:
        entries.append(_entry(51044, _SRATIONAL, (float(info.frame_rate),)))
    if info.timecodes is not None:
        entries.append(_entry(51043, _BYTE, list(info.timecodes)))
    if info.calibration_1:
        entries.append(_entry(50723, _SRATIONAL, info.calibration_1))
    if info.calibration_2:
        entries.append(_entry(50724, _SRATIONAL, info.calibration_2))
    if info.analog_balance is not None:
        entries.append(_entry(50727, _RATIONAL, info.analog_balance))
    if info.opcode_list_3:
        entries.append(_entry(51022, _UNDEFINED, info.opcode_list_3))
    return entries


def _crop_entries(crop: tuple[int, int, int, int]) -> list[tuple[int, int, int, bytes]]:
    left, top, width, height = crop
    return [_entry(50719, _LONG, [left, top]), _entry(50720, _LONG, [width, height])]


def tag_value_offset(header: bytes, tag: int) -> int | None:
    """Where a tag's out-of-line value lies in a little-endian TIFF header (None when absent or inline)."""
    (ifd,) = struct.unpack_from("<I", header, 4)
    (count,) = struct.unpack_from("<H", header, ifd)
    for index in range(count):
        number, kind, n, value = struct.unpack_from("<HHII", header, ifd + 2 + 12 * index)
        if number == tag:
            return value if _SIZES.get(kind, 1) * n > 4 else None
    return None


def read_dng_tags(path: Path) -> dict[int, Any]:
    """The first IFD's tags, decoded (numbers as tuples, text as str, undefined as bytes)."""
    with path.open("rb") as f:
        head = f.read(8)
        if head[:4] != b"II*\0":
            raise ValueError(f"{path.name} is not a little-endian TIFF")
        (ifd,) = struct.unpack_from("<I", head, 4)
        f.seek(ifd)
        (count,) = struct.unpack("<H", f.read(2))
        raw_entries = [struct.unpack("<HHI4s", f.read(12)) for _ in range(count)]
        tags: dict[int, Any] = {}
        for tag, kind, n, value in raw_entries:
            size = _SIZES.get(kind, 1) * n
            if size <= 4:
                data = value[:size]
            else:
                f.seek(struct.unpack("<I", value)[0])
                data = f.read(size)
            tags[tag] = _decode(kind, n, data)
    return tags


def _decode(kind: int, n: int, data: bytes) -> Any:
    if kind == _ASCII:
        return data.rstrip(b"\0").decode("ascii", "replace")
    if kind == _UNDEFINED:
        return data
    if kind in (_RATIONAL, _SRATIONAL):
        pairs = struct.unpack(f"<{2 * n}{'i' if kind == _SRATIONAL else 'I'}", data)
        return tuple(pairs[i] / pairs[i + 1] for i in range(0, len(pairs), 2))
    fmt = {_BYTE: "B", _SHORT: "H", _LONG: "I", _FLOAT: "f", _DOUBLE: "d"}[kind]
    return struct.unpack(f"<{n}{fmt}", data)
