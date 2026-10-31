"""Colour encodings: a registered gamut and transfer, and conversions to and from scene-linear AP1.

A `ColourEncoding` is named by its `key`: the legacy name for the three pairs MCRAWsome knew before
Milestone 7 (`acescct`, `davinci`, `bt709`), and `<gamut id>/<transfer id>` for every other pair, with
`@<EI>` after ARRI LogC3 at an exposure index other than 800. Frame jobs carry the key, so a legacy pair
keeps its picture fingerprint and its exact conversion.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache
from typing import Literal

import numpy as np
import numpy.typing as npt

from mcrawsome.colour.spaces import AP1_TO_DAVINCI_WG, AP1_TO_REC709, CAMERA_NATIVE, GAMUTS, gamut_conversion
from mcrawsome.colour.transfer import (
    LOGC3_EI,
    TRANSFERS,
    Transfer,
    acescct_decode,
    acescct_encode,
    bt709_oetf_inverse,
    davinci_intermediate_decode,
    davinci_intermediate_encode,
    hlg_decode,
    transfer,
)

Frame = npt.NDArray[np.float32]

GamutId = Literal[
    "rec709", "rec2020", "v-gamut", "awg3", "davinci-wg", "ap1", "ap0", "cinema-gamut", "s-gamut3",
    "s-gamut3-cine", "display-p3", "apple-wide-gamut", "camera-native",
]  # fmt: skip
TransferId = Literal[
    "bt709", "srgb", "pq", "hlg", "cineon", "v-log", "davinci-intermediate", "acescct", "canon-log3",
    "s-log3", "apple-log", "samsung-log", "logc3", "f-log", "f-log2", "linear",
]  # fmt: skip
# The transfers that take MCRAWsome's scene linear, so can encode a log output (not PQ or HLG).
LogTransferId = Literal[
    "bt709", "srgb", "cineon", "v-log", "davinci-intermediate", "acescct", "canon-log3", "s-log3",
    "apple-log", "samsung-log", "logc3", "f-log", "f-log2", "linear",
]  # fmt: skip

LOGC3_DEFAULT_EI = 800  # ARRI's default, and the curve LogC3 means without an EI

# Reference white of an HLG signal: 75 % HLG, E = 0.26496 (ITU-R BT.2408, recalled); and of PQ, 203 cd/m2.
HLG_REFERENCE_WHITE = float(hlg_decode(0.75))
PQ_REFERENCE_WHITE = 203.0


@dataclass(frozen=True)
class ColourEncoding:
    """A gamut id (`GAMUTS`) and a transfer id (`TRANSFERS`); `ei` is ARRI LogC3's exposure index."""

    gamut: str
    transfer: str
    ei: int = LOGC3_DEFAULT_EI

    def __post_init__(self) -> None:
        if self.ei != LOGC3_DEFAULT_EI and self.transfer != "logc3":
            raise ValueError(f"only ARRI LogC3 takes an exposure index, not {self.transfer}")
        if self.ei not in LOGC3_EI:
            raise ValueError(f"ARRI LogC3 has no EI {self.ei}; known: {', '.join(map(str, LOGC3_EI))}")

    @property
    def key(self) -> str:
        for name, pair in LEGACY.items():
            if pair == self:
                return name
        ei = f"@{self.ei}" if self.ei != LOGC3_DEFAULT_EI else ""
        return f"{self.gamut}/{self.transfer}{ei}"

    @property
    def name(self) -> str:
        ei = f" (EI {self.ei})" if self.ei != LOGC3_DEFAULT_EI else ""
        return f"{GAMUTS[self.gamut].name} / {TRANSFERS[self.transfer].name}{ei}"

    def curve(self) -> Transfer:
        """The transfer, at its exposure index."""
        return transfer(self.transfer, ei=self.ei if self.transfer == "logc3" else None)


LEGACY: dict[str, ColourEncoding] = {
    "davinci": ColourEncoding("davinci-wg", "davinci-intermediate"),
    "bt709": ColourEncoding("rec709", "bt709"),
    "acescct": ColourEncoding("ap1", "acescct"),
}

# A log output's gamut when output.log_gamut is auto: the gamut each curve's maker pairs it with.
LOG_GAMUTS: dict[str, str] = {
    "davinci-intermediate": "davinci-wg",
    "acescct": "ap1",
    "linear": "ap1",
    "bt709": "rec709",
    "srgb": "rec709",
    "cineon": "rec709",
    "v-log": "v-gamut",
    "logc3": "awg3",
    "s-log3": "s-gamut3-cine",
    "canon-log3": "cinema-gamut",
    "apple-log": "rec2020",
    "samsung-log": "rec2020",
    "f-log": "rec2020",
    "f-log2": "rec2020",
}


def encoding(key: str) -> ColourEncoding:
    """The encoding a key names. ValueError for anything else."""
    if key in LEGACY:
        return LEGACY[key]
    gamut, _, rest = key.partition("/")
    curve, at, ei = rest.partition("@")
    if gamut not in GAMUTS or curve not in TRANSFERS or (at and (not ei.isdecimal() or curve != "logc3")):
        raise ValueError(f"{key!r} is not a colour encoding (a legacy name or <gamut>/<transfer>)")
    return ColourEncoding(gamut, curve, int(ei) if at else LOGC3_DEFAULT_EI)


def colour_labels(key: str | None, source: str = "Embedded metadata") -> dict[str, str]:
    """Recorded video colour facts, with the space and transfer named independently."""
    if key is None:
        return {
            "colour_space": "Unknown",
            "transfer_function": "Unknown",
            "encoding_source": "Not identified",
        }
    pair = encoding(key)
    gamut = GAMUTS[pair.gamut]
    return {
        "colour_space": " / ".join(gamut.names) if gamut.names else gamut.name,
        "transfer_function": TRANSFERS[pair.transfer].name,
        "transfer_id": pair.transfer,
        "encoding_source": source,
    }


def log_encoding(curve: str, gamut: str = "auto", ei: int = LOGC3_DEFAULT_EI) -> ColourEncoding:
    """A log output's encoding from output.log (a transfer id, or `davinci`), output.log_gamut and, for
    LogC3, output.log_ei."""
    curve = "davinci-intermediate" if curve == "davinci" else curve
    return ColourEncoding(LOG_GAMUTS[curve] if gamut == "auto" else gamut, curve, ei)


@cache
def _matrix(src: str, dst: str) -> npt.NDArray[np.float32]:
    return gamut_conversion(src, dst).astype(np.float32)


_DWG_TO_AP1 = np.linalg.inv(AP1_TO_DAVINCI_WG)
_REC709_TO_AP1 = np.linalg.inv(AP1_TO_REC709)


def decode_scene(code: npt.ArrayLike, curve: str | Transfer) -> npt.NDArray[np.float64]:
    """A transfer's code values to MCRAWsome's scene linear (1.0 = diffuse white); PQ and HLG by their
    reference whites, 203 cd/m2 and 75 % HLG."""
    t = transfer(curve) if isinstance(curve, str) else curve
    if t.scale == "nits":
        return t.decode(code) / PQ_REFERENCE_WHITE
    if t.scale == "hlg-e":
        return t.decode(code) / HLG_REFERENCE_WHITE
    return t.decode_scene(code)


def to_linear_ap1(rgb: Frame, key: str) -> Frame:
    """Values in an encoding to scene-linear AP1 (float32, for whole frames). Camera Native is taken as
    AP1: a video has no camera matrix to undo."""
    if key == "acescct":
        return np.asarray(acescct_decode(rgb), dtype=np.float32)
    if key == "davinci":
        linear = np.asarray(davinci_intermediate_decode(rgb), dtype=np.float32)
        return np.asarray(linear @ _DWG_TO_AP1.T.astype(np.float32), dtype=np.float32)
    if key == "bt709":
        linear = np.asarray(bt709_oetf_inverse(rgb), dtype=np.float32)
        return np.asarray(linear @ _REC709_TO_AP1.T.astype(np.float32), dtype=np.float32)
    pair = encoding(key)
    linear = np.asarray(decode_scene(rgb, pair.curve()), dtype=np.float32)
    if pair.gamut in ("ap1", CAMERA_NATIVE):
        return linear
    return np.asarray(linear @ _matrix(pair.gamut, "ap1").T, dtype=np.float32)


def decode_ap1(rgb: npt.ArrayLike, key: str) -> npt.NDArray[np.float64]:
    """to_linear_ap1 in float64, for baking tables."""
    pair = encoding(key)
    linear = decode_scene(rgb, pair.curve())
    if pair.gamut in ("ap1", CAMERA_NATIVE):
        return linear
    return np.asarray(linear @ gamut_conversion(pair.gamut, "ap1").T, dtype=np.float64)


def from_linear_ap1(linear: Frame, key: str, native: npt.ArrayLike | None = None) -> npt.NDArray[np.floating]:
    """Scene-linear AP1 to an encoding's code values. Camera Native multiplies by `native` (AP1 to the
    camera's RGB, 3x3), or passes the values through without one. PQ and HLG are refused: they are not
    scene-linear curves."""
    if key == "acescct":
        return acescct_encode(linear)
    if key == "davinci":
        return davinci_intermediate_encode(linear @ AP1_TO_DAVINCI_WG.T.astype(np.float32))
    pair = encoding(key)
    rgb = linear
    if pair.gamut == CAMERA_NATIVE:
        if native is not None:
            rgb = linear @ np.asarray(native, dtype=np.float32).T
    elif pair.gamut != "ap1":
        rgb = linear @ _matrix("ap1", pair.gamut).T
    return pair.curve().encode_scene(rgb)
