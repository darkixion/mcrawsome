"""Transfer functions. Linear values are relative (1.0 = diffuse white) except PQ, which uses nits.

Code values are normalised full range (10-bit code value / 1023) unless a curve's docstring says
otherwise. Each curve uses its maker's formula and constants as printed; `docs/colour-sources.md`
lists the documents. Page numbers are the PDF's own pages; "recalled" marks a curve whose document
is not among the owner's copies.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from functools import cache, partial
from typing import Literal

import cv2
import numpy as np
import numpy.typing as npt

Array = npt.NDArray[np.float64]
ArrayLike = npt.ArrayLike


def _a(x: ArrayLike) -> Array:
    return np.asarray(x, dtype=np.float64)


def bt709_oetf(x: ArrayLike) -> Array:
    v = _a(x)
    return np.where(v < 0.018, 4.5 * v, 1.099 * np.power(np.maximum(v, 0.0), 0.45) - 0.099)


def bt709_oetf_inverse(x: ArrayLike) -> Array:
    v = _a(x)
    return np.where(v < 0.081, v / 4.5, np.power((np.maximum(v, 0.0) + 0.099) / 1.099, 1 / 0.45))


_CCT_CUT_LIN = 0.0078125
_CCT_A, _CCT_B = 10.5402377416545, 0.0729055341958355


def acescct_encode(x: ArrayLike) -> Array:
    v = _a(x)
    return np.where(v <= _CCT_CUT_LIN, _CCT_A * v + _CCT_B, (np.log2(np.maximum(v, 1e-10)) + 9.72) / 17.52)


def acescct_encode32(x: npt.NDArray[np.float32]) -> npt.NDArray[np.float32]:
    """acescct_encode in float32 throughout, for previews (within float32 rounding of it)."""
    out = np.asarray(cv2.log(np.maximum(x, np.float32(1e-10))), dtype=np.float32)  # natural log, threaded
    out *= np.float32(1 / (np.log(2.0) * 17.52))
    out += np.float32(9.72 / 17.52)
    low = x <= np.float32(_CCT_CUT_LIN)
    if low.any():
        out[low] = np.float32(_CCT_A) * x[low] + np.float32(_CCT_B)
    return out


def acescct_decode(x: ArrayLike) -> Array:
    v = _a(x)
    return np.where(
        v <= 0.155251141552511, (v - _CCT_B) / _CCT_A, np.exp2(np.minimum(v, 1.468) * 17.52 - 9.72)
    )


_M1, _M2 = 2610 / 16384, 2523 / 4096 * 128
_C1, _C2, _C3 = 3424 / 4096, 2413 / 4096 * 32, 2392 / 4096 * 32


def pq_encode(nits: ArrayLike) -> Array:
    y = np.power(np.clip(_a(nits) / 10000.0, 0.0, 1.0), _M1)
    return np.power((_C1 + _C2 * y) / (1 + _C3 * y), _M2)


def pq_decode(code: ArrayLike) -> Array:
    e = np.power(np.clip(_a(code), 0.0, 1.0), 1 / _M2)
    return 10000.0 * np.power(np.maximum(e - _C1, 0.0) / (_C2 - _C3 * e), 1 / _M1)


_DI_A, _DI_B, _DI_C, _DI_M = 0.0075, 7.0, 0.07329248, 10.44426855
_DI_LIN_CUT, _DI_LOG_CUT = 0.00262409, 0.02740668


def davinci_intermediate_encode(x: ArrayLike) -> Array:
    v = _a(x)
    return np.where(v <= _DI_LIN_CUT, v * _DI_M, (np.log2(np.maximum(v, 0.0) + _DI_A) + _DI_B) * _DI_C)


def davinci_intermediate_decode(x: ArrayLike) -> Array:
    v = _a(x)
    return np.where(v <= _DI_LOG_CUT, v / _DI_M, np.exp2(v / _DI_C - _DI_B) - _DI_A)


def linear_encode(x: ArrayLike) -> Array:
    return _a(x).copy()


def linear_decode(x: ArrayLike) -> Array:
    return _a(x).copy()


# IEC 61966-2-1:1999, Multimedia systems and equipment, Colour measurement and management, Part 2-1:
# Default RGB colour space, sRGB, section 5.2 (the piece-wise curve, not a pure 2.2 power). Recalled.


def srgb_encode(x: ArrayLike) -> Array:
    """sRGB: 1.0 is the display's white; the linear segment carries on below 0."""
    v = _a(x)
    return np.where(v <= 0.0031308, 12.92 * v, 1.055 * np.power(np.maximum(v, 0.0031308), 1 / 2.4) - 0.055)


def srgb_decode(x: ArrayLike) -> Array:
    v = _a(x)
    return np.where(v <= 0.04045, v / 12.92, np.power((np.maximum(v, 0.04045) + 0.055) / 1.055, 2.4))


# ITU-R BT.2100-3 (02/2025), table 5, pages 7 and 8: the HLG OETF and its inverse (b and c computed
# from a, as note 5c gives them). The OOTF is not applied here.
_HLG_A = 0.17883277
_HLG_B = 1 - 4 * _HLG_A
_HLG_C = 0.5 - _HLG_A * float(np.log(4 * _HLG_A))


def hlg_encode(x: ArrayLike) -> Array:
    """HLG OETF on scene light E normalised as BT.2100 does, 0 to 1 (not 1.0 = diffuse white).

    BT.2408 puts HDR reference white at 75 % HLG, which is E = 0.26496; 18 % grey is 0.18 times that.
    Negative light is clipped to 0, where the curve is not defined.
    """
    e = np.maximum(_a(x), 0.0)
    return np.where(e <= 1 / 12, np.sqrt(3 * e), _HLG_A * np.log(np.maximum(12 * e - _HLG_B, 1e-12)) + _HLG_C)


def hlg_decode(x: ArrayLike) -> Array:
    v = np.maximum(_a(x), 0.0)
    return np.where(v <= 0.5, v * v / 3, (np.exp((np.maximum(v, 0.5) - _HLG_C) / _HLG_A) + _HLG_B) / 12)


# Kodak, Conversion of 10-bit Log Film Data To 8-bit Linear or Video Data for The Cineon Digital Film
# System, Version 2.1 (1995-07-26): reference black 95, reference white 685, 0.002 printing density
# per code value (page 4), negative gamma 0.6 and the black offset (section 5.3, page 7).
# This is the normalised linear LUT, without the output clipping or optional softclip.
_CIN_BLACK, _CIN_WHITE, _CIN_STEP = 95.0, 685.0, 0.002 / 0.6
_CIN_OFFSET = float(10.0 ** ((_CIN_BLACK - _CIN_WHITE) * _CIN_STEP))


def cineon_encode(x: ArrayLike) -> Array:
    """Cineon printing density. 1.0 (diffuse white) is code value 685, 0 is code value 95.

    Kodak's section 5.3 linear LUT (page 7), inverted, with no output clipping or softclip.
    Below black the curve carries on down to a linear value of -0.0109, where the log reaches minus
    infinity; values below that are held at the lowest finite code.
    """
    arg = _a(x) * (1 - _CIN_OFFSET) + _CIN_OFFSET
    return (_CIN_WHITE + np.log10(np.maximum(arg, 1e-12)) / _CIN_STEP) / 1023


def cineon_decode(x: ArrayLike) -> Array:
    return (np.power(10.0, (_a(x) * 1023 - _CIN_WHITE) * _CIN_STEP) - _CIN_OFFSET) / (1 - _CIN_OFFSET)


# Panasonic, V-Log/V-Gamut Reference Manual, Rev.1.0 (2014-11-28), section 3, page 4.
_VL_CUT1, _VL_CUT2 = 0.01, 0.181
_VL_B, _VL_C, _VL_D = 0.00873, 0.241514, 0.598206


def vlog_encode(x: ArrayLike) -> Array:
    """V-Log. Panasonic's input is reflection (0.18 = 18 % grey), the same scale as MCRAWsome's linear."""
    v = _a(x)
    return np.where(v < _VL_CUT1, 5.6 * v + 0.125, _VL_C * np.log10(np.maximum(v, _VL_CUT1) + _VL_B) + _VL_D)


def vlog_decode(x: ArrayLike) -> Array:
    v = _a(x)
    return np.where(v < _VL_CUT2, (v - 0.125) / 5.6, np.power(10.0, (v - _VL_D) / _VL_C) - _VL_B)


# Sony, Technical Summary for S-Gamut3.Cine/S-Log3 and S-Gamut3/S-Log3, appendix, page 6 (formulas on
# 10-bit full-range code values).
_SL3_CUT = 0.01125000
_SL3_CODE_CUT = 171.2102946929


def slog3_encode(x: ArrayLike) -> Array:
    """S-Log3. Sony's input is reflection (0.18 = 18 % grey), the same scale as MCRAWsome's linear."""
    v = _a(x)
    log = 420.0 + np.log10((np.maximum(v, _SL3_CUT) + 0.01) / (0.18 + 0.01)) * 261.5
    return np.where(v >= _SL3_CUT, log, v * (_SL3_CODE_CUT - 95.0) / _SL3_CUT + 95.0) / 1023.0


def slog3_decode(x: ArrayLike) -> Array:
    cv = _a(x) * 1023.0
    log = np.power(10.0, (cv - 420.0) / 261.5) * (0.18 + 0.01) - 0.01
    return np.where(cv >= _SL3_CODE_CUT, log, (cv - 95.0) * _SL3_CUT / (_SL3_CODE_CUT - 95.0))


# Canon, Canon Log Gamma Curves white paper (Description of the Canon Log, Canon Log 2 and Canon Log 3
# Gamma Curves, 2018-11-01), appendix [3], page 10. Canon's x is scene linear with reflection = x * 0.9
# (so 1.0 = 90 % reflectance), and its y is full range: 10-bit code value / 1023 (appendix [4]).
_CL3_A, _CL3_B, _CL3_SCALE = 0.36726845, 14.98325, 0.9
_CL3_D_LOW, _CL3_D_HIGH = 0.12783901, 0.12240537
_CL3_SLOPE, _CL3_OFFSET = 1.9754798, 0.12512219
_CL3_CUT = 0.014
_CL3_CODE_LOW, _CL3_CODE_HIGH = 0.097465473, 0.15277891


def canon_log3_encode(x: ArrayLike) -> Array:
    """Canon Log 3. Input is reflection (0.18 = 18 % grey), divided by 0.9 for Canon's formula; output
    is Canon's full-range signal, code value / 1023, like the other logs. The curve is defined below
    black by Canon's mirrored log segment."""
    v = _a(x) / _CL3_SCALE
    m = np.abs(v)
    y = np.where(
        v < -_CL3_CUT,
        -_CL3_A * np.log10(m * _CL3_B + 1) + _CL3_D_LOW,
        np.where(
            v <= _CL3_CUT, _CL3_SLOPE * v + _CL3_OFFSET, _CL3_A * np.log10(m * _CL3_B + 1) + _CL3_D_HIGH
        ),
    )
    return np.asarray(y, dtype=np.float64)


def canon_log3_decode(x: ArrayLike) -> Array:
    y = _a(x)
    low = -(np.power(10.0, (_CL3_D_LOW - y) / _CL3_A) - 1) / _CL3_B
    high = (np.power(10.0, (y - _CL3_D_HIGH) / _CL3_A) - 1) / _CL3_B
    mid = (y - _CL3_OFFSET) / _CL3_SLOPE
    return np.where(y < _CL3_CODE_LOW, low, np.where(y <= _CL3_CODE_HIGH, mid, high)) * _CL3_SCALE


# Apple, Apple Log Profile White Paper (2023-09), page 5; the Apple Log 2 White Paper (2025-09,
# version 1.1), page 5, gives the same curve and constants for Apple Log 2.
_AL_R0, _AL_RT = -0.05641088, 0.01
_AL_SIGMA, _AL_BETA, _AL_GAMMA, _AL_DELTA = 47.28711236, 0.00964052, 0.08550479, 0.69336945
_AL_PT = _AL_SIGMA * (_AL_RT - _AL_R0) ** 2


def apple_log_encode(x: ArrayLike) -> Array:
    """Apple Log. Apple's R is scene reflection (0.18 = 18 % grey), the same scale as MCRAWsome's
    linear; its output P is Apple's normalised signal, kept as is. Below R0 = -0.0564 it holds at 0."""
    r = _a(x)
    log = _AL_GAMMA * np.log2(np.maximum(r, _AL_RT) + _AL_BETA) + _AL_DELTA
    return np.where(r >= _AL_RT, log, np.where(r >= _AL_R0, _AL_SIGMA * (r - _AL_R0) ** 2, 0.0))


def apple_log_decode(x: ArrayLike) -> Array:
    p = _a(x)
    log = np.exp2((p - _AL_DELTA) / _AL_GAMMA) - _AL_BETA
    return np.where(p >= _AL_PT, log, np.sqrt(np.maximum(p, 0.0) / _AL_SIGMA) + _AL_R0)


# Samsung, Samsung Log White Paper (2025), "Samsung Log transfer function", page 5: two log10 segments
# meeting at x_t, held at 0 below x_0. Samsung Log uses BT.2020 primaries (page 7), the `rec2020` gamut.
_SL_X0, _SL_XT, _SL_YT = -0.05, 0.01, 0.206561909
_SL_A1, _SL_B1, _SL_G1 = 0.258984868, 0.0003645, 0.720504856
_SL_A2, _SL_B2, _SL_G2 = -0.20942, 0.016904, -0.24597


def samsung_log_encode(x: ArrayLike) -> Array:
    """Samsung Log. Samsung's x is scene reflection (0.18 = 18 % grey), the same scale as MCRAWsome's
    linear; its output y is Samsung's normalised signal, 10-bit full range code value / 1023. Below
    x_0 = -0.05 it holds at 0.

    Samsung's printed constants leave the lower segment 0.0000036 above the upper one at x_t = 0.01, so
    a sliver of linear values just below x_t does not round-trip; the formula is kept as printed.
    """
    v = _a(x)
    high = _SL_A1 * np.log10(np.maximum(v, _SL_XT) + _SL_B1) + _SL_G1
    low = _SL_A2 * np.log10(-np.clip(v, _SL_X0, _SL_XT) + _SL_B2) + _SL_G2
    return np.where(v >= _SL_XT, high, np.where(v >= _SL_X0, low, 0.0))


def samsung_log_decode(x: ArrayLike) -> Array:
    y = _a(x)
    high = np.power(10.0, (y - _SL_G1) / _SL_A1) - _SL_B1
    low = -np.power(10.0, (y - _SL_G2) / _SL_A2) + _SL_B2
    return np.where(y >= _SL_YT, high, np.where(y >= 0.0, low, _SL_X0))


# ARRI, ALEXA Log C Curve Usage in VFX (Harald Brendel, 2017-03-09), page 9: the SUP 3.x table for
# exposure values, per EI, cut, a, b, c, d, e, f and e * cut + f as printed.
LOGC3_EI: dict[int, tuple[float, float, float, float, float, float, float, float]] = {
    160: (0.005561, 5.555556, 0.080216, 0.269036, 0.381991, 5.842037, 0.092778, 0.125266),
    200: (0.006208, 5.555556, 0.076621, 0.266007, 0.382478, 5.776265, 0.092782, 0.128643),
    250: (0.006871, 5.555556, 0.072941, 0.262978, 0.382966, 5.710494, 0.092786, 0.132021),
    320: (0.007622, 5.555556, 0.068768, 0.259627, 0.383508, 5.637732, 0.092791, 0.135761),
    400: (0.008318, 5.555556, 0.064901, 0.256598, 0.383999, 5.571960, 0.092795, 0.139142),
    500: (0.009031, 5.555556, 0.060939, 0.253569, 0.384493, 5.506188, 0.092800, 0.142526),
    640: (0.009840, 5.555556, 0.056443, 0.250219, 0.385040, 5.433426, 0.092805, 0.146271),
    800: (0.010591, 5.555556, 0.052272, 0.247190, 0.385537, 5.367655, 0.092809, 0.149658),
    1000: (0.011361, 5.555556, 0.047996, 0.244161, 0.386036, 5.301883, 0.092814, 0.153047),
    1280: (0.012235, 5.555556, 0.043137, 0.240810, 0.386590, 5.229121, 0.092819, 0.156799),
    1600: (0.013047, 5.555556, 0.038625, 0.237781, 0.387093, 5.163350, 0.092824, 0.160192),
}


def _logc3(ei: int) -> tuple[float, float, float, float, float, float, float, float]:
    try:
        return LOGC3_EI[ei]
    except KeyError:
        raise ValueError(
            f"ARRI LogC3 has no parameters for EI {ei}; known: {', '.join(map(str, LOGC3_EI))}"
        ) from None


def logc3_encode(x: ArrayLike, ei: int = 800) -> Array:
    """ARRI LogC3. ARRI's input is linear scene exposure (0.18 = 18 % grey), the same scale as
    MCRAWsome's linear; output is ARRI's normalised signal, code value / 1023."""
    cut, a, b, c, d, e, f, _ = _logc3(ei)
    v = _a(x)
    return np.where(v > cut, c * np.log10(a * np.maximum(v, cut) + b) + d, e * v + f)


def logc3_decode(x: ArrayLike, ei: int = 800) -> Array:
    _, a, b, c, d, e, f, code_cut = _logc3(ei)
    t = _a(x)
    return np.where(t > code_cut, (np.power(10.0, (t - d) / c) - b) / a, (t - f) / e)


# Fujifilm, F-Log Data Sheet Ver.1.2 and F-Log2 Data Sheet Ver.1.1, section 2-3, page 2 of each.
_FLOG = (0.00089, 0.100537775223865, 0.555556, 0.009468, 0.344676, 0.790453, 8.735631, 0.092864)
_FLOG2 = (0.000889, 0.100686685370811, 5.555556, 0.064829, 0.245281, 0.384316, 8.799461, 0.092864)


def _flog_encode(x: ArrayLike, k: tuple[float, ...]) -> Array:
    cut1, _, a, b, c, d, e, f = k
    v = _a(x)
    return np.where(v >= cut1, c * np.log10(a * np.maximum(v, cut1) + b) + d, e * v + f)


def _flog_decode(x: ArrayLike, k: tuple[float, ...]) -> Array:
    _, cut2, a, b, c, d, e, f = k
    v = _a(x)
    return np.where(v >= cut2, (np.power(10.0, (v - d) / c) - b) / a, (v - f) / e)


def flog_encode(x: ArrayLike) -> Array:
    """F-Log. Fujifilm's input is reflection (0.18 = 18 % grey), the same scale as MCRAWsome's linear.

    Fujifilm's printed constants (e = 8.735631 in Ver.1.2) leave the linear segment 0.0001 above the
    log one at cut1 = 0.00089, so a sliver of linear values just below cut1 does not round-trip; the
    formula is kept as printed.
    """
    return _flog_encode(x, _FLOG)


def flog_decode(x: ArrayLike) -> Array:
    return _flog_decode(x, _FLOG)


def flog2_encode(x: ArrayLike) -> Array:
    """F-Log2. Fujifilm's input is reflection (0.18 = 18 % grey), the same scale as MCRAWsome's linear."""
    return _flog_encode(x, _FLOG2)


def flog2_decode(x: ArrayLike) -> Array:
    return _flog_decode(x, _FLOG2)


Curve = Callable[[ArrayLike], Array]

# What a curve's linear side means. "scene": MCRAWsome's scene linear, 1.0 = 100 % diffuse white and
# 0.18 = 18 % grey. "nits": absolute display light in cd/m2 (PQ). "hlg-e": BT.2100's HLG scene light E,
# 0 to 1, where reference white is E = 0.26496 (HLG). Only "scene" curves take MCRAWsome's linear as is.
Scale = Literal["scene", "nits", "hlg-e"]


@dataclass(frozen=True)
class Transfer:
    """A transfer function and the code value its maker gives 18 % grey.

    `grey` is the linear value of 18 % grey in the curve's own input scale (0.18 for scene-linear curves)
    and `grey_code` the code value it encodes to. `breaks` are the linear breakpoints between segments.
    `scale` says what the linear side means; `encode_scene` and `decode_scene` refuse curves whose scale
    is not MCRAWsome's scene linear, so scene values cannot reach PQ or HLG without a conversion.
    """

    id: str
    name: str
    encode: Curve
    decode: Curve
    grey: float
    reference: str
    breaks: tuple[float, ...] = ()
    scale: Scale = "scene"
    grey_code: float = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "grey_code", float(self.encode(self.grey)))

    def encode_scene(self, x: ArrayLike) -> Array:
        """Encode MCRAWsome scene-linear values; refused unless the curve takes scene linear."""
        self._require_scene()
        return self.encode(x)

    def decode_scene(self, code: ArrayLike) -> Array:
        """Decode to MCRAWsome scene-linear values; refused unless the curve gives scene linear."""
        self._require_scene()
        return self.decode(code)

    def _require_scene(self) -> None:
        if self.scale != "scene":
            raise ValueError(
                f"{self.name} works in {self.scale!r}, not scene linear: convert to its scale first"
            )


def _logc3_transfer(ei: int) -> Transfer:
    cut = _logc3(ei)[0]
    return Transfer(
        "logc3",
        "ARRI LogC3",
        partial(logc3_encode, ei=ei),
        partial(logc3_decode, ei=ei),
        0.18,
        f"ARRI, ALEXA Log C Curve Usage in VFX (2017-03-09), page 9, SUP 3.x, EI {ei}",
        (cut,),
    )


_HLG_GREY = 0.18 * float(hlg_decode(0.75))

TRANSFERS: dict[str, Transfer] = {
    t.id: t
    for t in (
        Transfer(
            "bt709",
            "BT.709",
            bt709_oetf,
            bt709_oetf_inverse,
            0.18,
            "ITU-R BT.709-6 (06/2015), item 1.2, page 5",
            (0.018,),
        ),
        Transfer(
            "srgb",
            "sRGB",
            srgb_encode,
            srgb_decode,
            0.18,
            "IEC 61966-2-1:1999, section 5.2 (recalled)",
            (0.0031308,),
        ),
        Transfer(
            "pq",
            "ST2084 PQ HDR",
            pq_encode,
            pq_decode,
            26.0,
            "ITU-R BT.2100-3 (02/2025), table 4, page 6; grey per ITU-R BT.2408-7, 26 cd/m2 (recalled)",
            scale="nits",
        ),
        Transfer(
            "hlg",
            "REC.2020 HLG HDR",
            hlg_encode,
            hlg_decode,
            _HLG_GREY,
            "ITU-R BT.2100-3 (02/2025), table 5, pages 7 and 8; grey per ITU-R BT.2408-7 (recalled)",
            (1 / 12,),
            scale="hlg-e",
        ),
        Transfer(
            "cineon",
            "Cineon Log",
            cineon_encode,
            cineon_decode,
            0.18,
            "Kodak, Conversion of 10-bit Log Film Data To 8-bit Linear or Video Data for The Cineon "
            "Digital Film System, Version 2.1 (1995-07-26), section 5.3, page 7",
        ),
        Transfer(
            "v-log",
            "Panasonic VLOG",
            vlog_encode,
            vlog_decode,
            0.18,
            "Panasonic, V-Log/V-Gamut Reference Manual, Rev.1.0 (2014-11-28), pages 3 and 4",
            (_VL_CUT1,),
        ),
        Transfer(
            "davinci-intermediate",
            "DaVinci Intermediate",
            davinci_intermediate_encode,
            davinci_intermediate_decode,
            0.18,
            "Blackmagic Design, DaVinci Resolve 17 Wide Gamut Intermediate, version 1.1 (2021-07-31), page 4",
            (_DI_LIN_CUT,),
        ),
        Transfer(
            "acescct",
            "ACES CCT",
            acescct_encode,
            acescct_decode,
            0.18,
            "Academy S-2016-001, ACEScct, version 1.0.3 (2020) (recalled)",
            (_CCT_CUT_LIN,),
        ),
        Transfer(
            "canon-log3",
            "Canon Log 3",
            canon_log3_encode,
            canon_log3_decode,
            0.18,
            "Canon, Canon Log Gamma Curves white paper (2018-11-01), appendix [3] and [4], page 10",
            (-_CL3_CUT * _CL3_SCALE, _CL3_CUT * _CL3_SCALE),
        ),
        Transfer(
            "s-log3",
            "Sony S-Log3",
            slog3_encode,
            slog3_decode,
            0.18,
            "Sony, Technical Summary for S-Gamut3.Cine/S-Log3 and S-Gamut3/S-Log3, page 6",
            (_SL3_CUT,),
        ),
        Transfer(
            "apple-log",
            "Apple Log",
            apple_log_encode,
            apple_log_decode,
            0.18,
            "Apple, Apple Log Profile White Paper (2023-09), pages 4, 5; Apple Log 2 White Paper (2025-09)",
            (_AL_RT,),
        ),
        Transfer(
            "samsung-log",
            "Samsung Log",
            samsung_log_encode,
            samsung_log_decode,
            0.18,
            "Samsung, Samsung Log White Paper (2025), pages 5 and 6",
            (_SL_XT,),
        ),
        _logc3_transfer(800),
        Transfer(
            "f-log",
            "F-Log",
            flog_encode,
            flog_decode,
            0.18,
            "Fujifilm, F-Log Data Sheet Ver.1.2, page 2",
            (_FLOG[0],),
        ),
        Transfer(
            "f-log2",
            "Fujifilm F-Log2",
            flog2_encode,
            flog2_decode,
            0.18,
            "Fujifilm, F-Log2 Data Sheet Ver.1.1, page 2",
            (_FLOG2[0],),
        ),
        Transfer("linear", "Linear", linear_encode, linear_decode, 0.18, "MCRAWsome: scene linear, no curve"),
    )
}


@cache
def transfer(transfer_id: str, *, ei: int | None = None) -> Transfer:
    """A registered transfer by id. ARRI LogC3 also takes its exposure index (`ei`, one of `LOGC3_EI`);
    the `TRANSFERS` entry is ARRI's suggested default, EI 800. No other curve takes an EI."""
    if ei is None or (transfer_id == "logc3" and ei == 800):
        try:
            return TRANSFERS[transfer_id]
        except KeyError:
            raise KeyError(f"unknown transfer {transfer_id!r}; known: {', '.join(TRANSFERS)}") from None
    if transfer_id != "logc3":
        raise ValueError(f"{transfer_id} has no exposure index; only logc3 takes one")
    return _logc3_transfer(ei)
