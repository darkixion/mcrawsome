"""RGB colour spaces, white points and conversion matrices."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import numpy.typing as npt

Matrix = npt.NDArray[np.float64]
XY = tuple[float, float]
Primaries = tuple[XY, XY, XY]

D50: XY = (0.34567, 0.35850)
D65: XY = (0.3127, 0.3290)
ACES_WHITE: XY = (0.32168, 0.33767)

AP1: Primaries = ((0.713, 0.293), (0.165, 0.830), (0.128, 0.044))
REC709: Primaries = ((0.640, 0.330), (0.300, 0.600), (0.150, 0.060))
REC2020: Primaries = ((0.708, 0.292), (0.170, 0.797), (0.131, 0.046))
DAVINCI_WG: Primaries = ((0.8000, 0.3130), (0.1682, 0.9877), (0.0790, -0.1155))
AP0: Primaries = ((0.7347, 0.2653), (0.0, 1.0), (0.0001, -0.0770))
V_GAMUT: Primaries = ((0.730, 0.280), (0.165, 0.840), (0.100, -0.030))
AWG3: Primaries = ((0.6840, 0.3130), (0.2210, 0.8480), (0.0861, -0.1020))
CINEMA_GAMUT: Primaries = ((0.7400, 0.2700), (0.1700, 1.1400), (0.0800, -0.1000))
S_GAMUT3: Primaries = ((0.730, 0.280), (0.140, 0.855), (0.100, -0.050))
S_GAMUT3_CINE: Primaries = ((0.766, 0.275), (0.225, 0.800), (0.089, -0.087))
DISPLAY_P3: Primaries = ((0.680, 0.320), (0.265, 0.690), (0.150, 0.060))  # D65 white, sRGB curve
APPLE_WIDE_GAMUT: Primaries = ((0.725, 0.301), (0.221, 0.814), (0.068, -0.076))

_BRADFORD = np.array([[0.8951, 0.2664, -0.1614], [-0.7502, 1.7135, 0.0367], [0.0389, -0.0685, 1.0296]])


def xy_to_xyz(xy: XY) -> npt.NDArray[np.float64]:
    x, y = xy
    return np.array([x / y, 1.0, (1 - x - y) / y])


def rgb_to_xyz(primaries: Primaries, white: XY) -> Matrix:
    p = np.stack([xy_to_xyz(c) for c in primaries], axis=1)
    scale = np.linalg.solve(p, xy_to_xyz(white))
    return np.asarray(p * scale, dtype=np.float64)


def bradford(src: XY, dst: XY) -> Matrix:
    return bradford_xyz(xy_to_xyz(src), xy_to_xyz(dst))


def bradford_xyz(src: npt.NDArray[np.float64], dst: npt.NDArray[np.float64]) -> Matrix:
    """Bradford adaptation between two white points given as XYZ (Y = 1)."""
    s, d = _BRADFORD @ src, _BRADFORD @ dst
    return np.asarray(np.linalg.inv(_BRADFORD) @ np.diag(d / s) @ _BRADFORD, dtype=np.float64)


def conversion(src: Primaries, src_white: XY, dst: Primaries, dst_white: XY) -> Matrix:
    adapt = np.eye(3) if src_white == dst_white else bradford(src_white, dst_white)
    return np.linalg.inv(rgb_to_xyz(dst, dst_white)) @ adapt @ rgb_to_xyz(src, src_white)


AP1_TO_XYZ = rgb_to_xyz(AP1, ACES_WHITE)
AP1_TO_REC709 = conversion(AP1, ACES_WHITE, REC709, D65)
AP1_LUMINANCE: npt.NDArray[np.float64] = AP1_TO_XYZ[1].copy()
REC709_TO_XYZ = rgb_to_xyz(REC709, D65)
XYZ_TO_REC2020 = np.linalg.inv(rgb_to_xyz(REC2020, D65))
AP1_TO_DAVINCI_WG = conversion(AP1, ACES_WHITE, DAVINCI_WG, D65)


@dataclass(frozen=True)
class Gamut:
    """An RGB gamut as its maker publishes it. Camera Native has no primaries: it means no conversion."""

    id: str
    name: str
    primaries: Primaries | None
    white: XY | None
    reference: str
    names: tuple[str, ...] = ()

    def to_xyz(self) -> Matrix:
        if self.primaries is None or self.white is None:
            raise ValueError(f"{self.name} has no matrix: it means the camera's own RGB, unconverted")
        return rgb_to_xyz(self.primaries, self.white)

    def from_xyz(self) -> Matrix:
        return np.asarray(np.linalg.inv(self.to_xyz()), dtype=np.float64)


CAMERA_NATIVE = "camera-native"

_BT709 = "ITU-R BT.709-6 (06/2015), Parameter values for the HDTV standards, items 1.3 and 1.4, page 5"
GAMUTS: dict[str, Gamut] = {
    g.id: g
    for g in (
        Gamut(
            "rec709",
            "sRGB",
            REC709,
            D65,
            _BT709 + "; IEC 61966-2-1:1999 (sRGB, recalled)",
            ("sRGB", "BT.709"),
        ),
        Gamut("rec2020", "Rec.2020", REC2020, D65, "ITU-R BT.2020-2 (10/2015), table 3, page 5"),
        Gamut(
            "v-gamut",
            "Panasonic Wide Gamut",
            V_GAMUT,
            D65,
            "Panasonic, V-Log/V-Gamut Reference Manual, Rev.1.0 (2014-11-28), section 4, pages 5 and 6",
        ),
        Gamut(
            "awg3",
            "ARRI Wide Gamut 3",
            AWG3,
            D65,
            "ARRI, ALEXA Log C Curve Usage in VFX (Harald Brendel, 2017-03-09), page 10",
        ),
        Gamut(
            "davinci-wg",
            "DaVinci Wide Gamut",
            DAVINCI_WG,
            D65,
            "Blackmagic Design, DaVinci Resolve 17 Wide Gamut Intermediate, version 1.1 (2021-07-31), page 3",
        ),
        Gamut(
            "ap1",
            "ACES AP1",
            AP1,
            ACES_WHITE,
            "Academy TB-2014-004, ACEScg (2014), section 4 (recalled)",
        ),
        Gamut(
            "ap0",
            "ACES AP0 (ACES2065-1)",
            AP0,
            ACES_WHITE,
            "SMPTE ST 2065-1:2012, ACES, section 4 (recalled); primaries as Sony, page 7",
        ),
        Gamut(
            "cinema-gamut",
            "Canon Cinema",
            CINEMA_GAMUT,
            D65,
            "Canon, Cinema Gamut (recalled: Canon's Log Gamma Curves white paper, 2018-11-01, lacks it)",
        ),
        Gamut(
            "s-gamut3",
            "Sony S-Gamut3",
            S_GAMUT3,
            D65,
            "Sony, Technical Summary for S-Gamut3.Cine/S-Log3 and S-Gamut3/S-Log3, page 7",
        ),
        Gamut(
            "s-gamut3-cine",
            "Sony S-Gamut3 (Cinema)",
            S_GAMUT3_CINE,
            D65,
            "Sony, Technical Summary for S-Gamut3.Cine/S-Log3 and S-Gamut3/S-Log3, page 7",
        ),
        Gamut(
            "display-p3",
            "Display P3",
            DISPLAY_P3,
            D65,
            "SMPTE EG 432-1:2010 P3 primaries, D65 white; as Samsung Log White Paper (2025), page 7",
        ),
        Gamut(
            "apple-wide-gamut",
            "Apple Log 2",
            APPLE_WIDE_GAMUT,
            D65,
            "Apple, Apple Log 2 White Paper (2025-09, version 1.1), page 6",
        ),
        Gamut(
            CAMERA_NATIVE, "Camera Native", None, None, "MCRAWsome: the camera's own RGB, no gamut conversion"
        ),
    )
}


def gamut_conversion(src: str, dst: str) -> Matrix:
    """The RGB to RGB matrix between two registered gamuts, Bradford-adapting between white points."""
    a, b = (_gamut(i) for i in (src, dst))
    if a.primaries is None or a.white is None or b.primaries is None or b.white is None:
        raise ValueError(f"{CAMERA_NATIVE} has no matrix: it means the camera's own RGB, unconverted")
    return conversion(a.primaries, a.white, b.primaries, b.white)


def _gamut(gamut_id: str) -> Gamut:
    try:
        return GAMUTS[gamut_id]
    except KeyError:
        raise KeyError(f"unknown gamut {gamut_id!r}; known: {', '.join(GAMUTS)}") from None
