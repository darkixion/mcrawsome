"""One frame as a scene-linear OpenEXR file: half-float RGB with its chromaticities.

Written for Blender, Nuke, Fusion and After Effects, which read the `chromaticities` attribute to know
the primaries and white point. A sequence is written one frame per call, so only one frame is held.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

import numpy as np
import numpy.typing as npt
import OpenEXR

from mcrawsome import __version__
from mcrawsome.colour.spaces import GAMUTS, Gamut

_HALF_MAX = 65504.0
_COMPRESSION = {"zip": OpenEXR.ZIP_COMPRESSION, "piz": OpenEXR.PIZ_COMPRESSION}


@dataclass(frozen=True)
class ExrSpace:
    id: str
    name: str
    gamut: Gamut


EXR_SPACES: dict[str, ExrSpace] = {
    "acescg": ExrSpace("acescg", "ACEScg (scene-linear AP1)", GAMUTS["ap1"]),
    "linear-rec709": ExrSpace("linear-rec709", "Linear Rec.709", GAMUTS["rec709"]),
}


TIMECODE_ATTRIBUTE = b"timeCode\x00timecode\x00\x08\x00\x00\x00"


def set_timecode(path: Path, address: bytes) -> None:
    """Write a frame's time address (as the DNG TimeCodes' first four bytes) into the timeCode attribute
    write_exr reserved, in place: the header keeps its size."""
    with path.open("r+b") as stream:
        head = stream.read(65536)
        at = head.find(TIMECODE_ATTRIBUTE)
        if at < 0:
            raise ValueError(f"{path.name} has no timeCode attribute to set")
        stream.seek(at + len(TIMECODE_ATTRIBUTE))
        stream.write(address[:4] + bytes(4))  # timeAndFlags, then empty user data


def write_exr(
    path: Path,
    rgb: npt.NDArray[np.floating],
    space: str = "acescg",
    compression: str = "zip",
    rate: Fraction | None = None,
) -> None:
    """Write `rgb` (height x width x 3, scene linear in `space`) as half floats, values beyond the half
    range clipped to it, with ZIP or PIZ compression."""
    if rgb.ndim != 3 or rgb.shape[2] != 3:
        raise ValueError(f"an EXR frame must be height x width x 3, not {' x '.join(map(str, rgb.shape))}")
    if space not in EXR_SPACES:
        raise ValueError(f"no EXR output space {space!r}; known: {', '.join(EXR_SPACES)}")
    if compression not in _COMPRESSION:
        raise ValueError(f"no EXR compression {compression!r}; known: {', '.join(_COMPRESSION)}")
    out = EXR_SPACES[space]
    primaries, white = out.gamut.primaries, out.gamut.white
    if primaries is None or white is None:
        raise ValueError(f"{out.gamut.name} has no primaries to write")
    (rx, ry), (gx, gy), (bx, by) = primaries
    header = {
        "compression": _COMPRESSION[compression],
        "type": OpenEXR.scanlineimage,
        "chromaticities": (rx, ry, gx, gy, bx, by, *white),
        "comment": f"MCRAWsome {__version__}: scene linear, {out.name}",
    }
    if rate is not None:  # time-of-day timecode: set per frame with set_timecode once placed
        header |= {"timeCode": OpenEXR.TimeCode(), "framesPerSecond": rate}
    # Cast first, so the only copy is the half-float one: values beyond the half range become
    # infinities in the cast and are then clipped back to plus or minus 65504 in place.
    with np.errstate(over="ignore"):
        half = rgb.astype(np.float16)
    np.clip(half, -_HALF_MAX, _HALF_MAX, out=half)
    with OpenEXR.File(header, {"RGB": half}) as exr:
        exr.write(str(path))
