"""Correlated colour temperature from Robertson's isotemperature lines (1968), in CIE 1960 uv: the DNG
white-balance method. Computed step for step as colour-science 0.4.7 computes it, which this replaces."""

from __future__ import annotations

import numpy as np

# Reciprocal megakelvin, u, v and slope of each isotemperature line (Wyszecki and Stiles, Color Science,
# table 1(3.11)), with Lindbloom's correction of u at 325 (0.24702 is 0.24792).
_LINES = np.array([
    (0, 0.18006, 0.26352, -0.24341), (10, 0.18066, 0.26589, -0.25479), (20, 0.18133, 0.26846, -0.26876),
    (30, 0.18208, 0.27119, -0.28539), (40, 0.18293, 0.27407, -0.30470), (50, 0.18388, 0.27709, -0.32675),
    (60, 0.18494, 0.28021, -0.35156), (70, 0.18611, 0.28342, -0.37915), (80, 0.18740, 0.28668, -0.40955),
    (90, 0.18880, 0.28997, -0.44278), (100, 0.19032, 0.29326, -0.47888), (125, 0.19462, 0.30141, -0.58204),
    (150, 0.19962, 0.30921, -0.70471), (175, 0.20525, 0.31647, -0.84901), (200, 0.21142, 0.32312, -1.0182),
    (225, 0.21807, 0.32909, -1.2168), (250, 0.22511, 0.33439, -1.4512), (275, 0.23247, 0.33904, -1.7298),
    (300, 0.24010, 0.34308, -2.0637), (325, 0.24792, 0.34655, -2.4681), (350, 0.25591, 0.34951, -2.9641),
    (375, 0.26400, 0.35200, -3.5814), (400, 0.27218, 0.35407, -4.3633), (425, 0.28039, 0.35577, -5.3762),
    (450, 0.28863, 0.35714, -6.7262), (475, 0.29685, 0.35823, -8.5955), (500, 0.30505, 0.35907, -11.324),
    (525, 0.31320, 0.35968, -15.628), (550, 0.32129, 0.36011, -23.325), (575, 0.32931, 0.36038, -40.770),
    (600, 0.33724, 0.36051, -116.45),
])  # fmt: skip
_R, _U, _V, _T = _LINES.T
_DU = 1.0 / np.hypot(1.0, _T)  # each line's direction, normalised
_DV = _T / np.hypot(1.0, _T)


def uv_to_cct(u: float, v: float) -> tuple[float, float]:
    """The correlated colour temperature (kelvin) and Duv of CIE 1960 `u`, `v`."""
    dt = -(u - _U[1:]) * _DV[1:] + (v - _V[1:]) * _DU[1:]
    crossed = np.flatnonzero(dt <= 0)
    i = int(crossed[0]) + 1 if len(crossed) else 30  # the first line the point lies beyond
    f = 0.0 if i == 1 else -min(dt[i - 1], 0.0) / (dt[i - 2] - min(dt[i - 1], 0.0))
    kelvin = 1.0e6 / (_R[i - 1] * f + _R[i] * (1 - f))
    du = _DU[i] * (1 - f) + _DU[i - 1] * f
    dv = _DV[i] * (1 - f) + _DV[i - 1] * f
    length = np.hypot(du, dv)
    locus_u, locus_v = _U[i - 1] * f + _U[i] * (1 - f), _V[i - 1] * f + _V[i] * (1 - f)
    duv = (u - locus_u) * (du / length) + (v - locus_v) * (dv / length)
    return float(kelvin), float(-duv)


def cct_to_uv(kelvin: float, duv: float) -> tuple[float, float]:
    """The CIE 1960 u, v of correlated colour temperature `kelvin` (kelvin) and `duv`."""
    r = 1.0e6 / kelvin
    beyond = np.flatnonzero(r < _R[1:])
    i = int(beyond[0]) if len(beyond) else 29  # the lines either side
    f = (_R[i + 1] - r) / (_R[i + 1] - _R[i])
    du = _DU[i] * f + _DU[i + 1] * (1 - f)
    dv = _DV[i] * f + _DV[i + 1] * (1 - f)
    length = np.hypot(du, dv)
    u = _U[i] * f + _U[i + 1] * (1 - f) + du / length * -duv
    v = _V[i] * f + _V[i + 1] * (1 - f) + dv / length * -duv
    return float(u), float(v)
