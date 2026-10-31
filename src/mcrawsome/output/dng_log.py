"""Log-encoded DNG values, read back through a DNG LinearizationTable (tag 50712).

A placeholder curve of MCRAWsome's own, kept here so the colour-science work on log curves can replace it:
`y = log2(1 + a v / W) / log2(1 + a)` over the whole stored range from 0 to the white level `W` (the black
pedestal included, so noise below black survives). `a` is chosen so the darkest code step is one linear
step: no code is spent on finer steps than the linear values have, and the rest of the codes go to the
shadows and mid-tones instead of the highlights. Any curve works for readers, because the table in each
file says how to linearise it.
"""

from __future__ import annotations

import functools
from typing import Literal

import numpy as np
import numpy.typing as npt

CURVE = "y = log2(1 + a v / W) / log2(1 + a), a so that the darkest code step is one linear step"
MIN_BITS = 6

Reduce = Literal["auto", 2, 4, 6, 8]


def _strength(white: int, out_bits: int) -> float:
    """`a` with a slope of one code per linear step at 0: a / ln(1 + a) = W / (2^bits - 1). 0 (linear) when
    the codes are as many as the linear steps."""
    ratio = white / (2**out_bits - 1)
    if ratio <= 1:
        return 0.0
    low, high = 1e-9, 1e9
    for _ in range(200):  # a / ln(1 + a) rises with a
        mid = (low * high) ** 0.5
        low, high = (mid, high) if mid / np.log1p(mid) < ratio else (low, mid)
    return float(low)


def _curve(v: npt.NDArray[np.float64], a: float) -> npt.NDArray[np.float64]:
    return v if a == 0 else np.asarray(np.log1p(a * v) / np.log1p(a), np.float64)


def _inverse(y: npt.NDArray[np.float64], a: float) -> npt.NDArray[np.float64]:
    return y if a == 0 else np.asarray(np.expm1(y * np.log1p(a)) / a, np.float64)


def encode(
    values: npt.NDArray[np.uint16], white: int, out_bits: int, seed: int | None
) -> npt.NDArray[np.uint16]:
    """Linear values (0 to `white`) as `out_bits` log codes. With a `seed`, one code of uniform noise is
    added before rounding (dithering), the same for the same seed, so a frame written again is identical."""
    y = _codes(white, out_bits)[values]
    if seed is not None:
        y += np.random.default_rng(seed).random(y.shape) - 0.5
    codes: npt.NDArray[np.uint16] = np.clip(np.rint(y), 0, 2**out_bits - 1).astype(np.uint16)
    return codes


@functools.lru_cache(maxsize=8)
def _codes(white: int, out_bits: int) -> npt.NDArray[np.float64]:
    """The unrounded code of every 16-bit value: one table look-up a sample instead of a logarithm, so a
    mounted frame encodes as it is read."""
    a, top = _strength(white, out_bits), 2**out_bits - 1
    values = np.arange(65536, dtype=np.float64)
    table: npt.NDArray[np.float64] = _curve(np.clip(values, 0, white) / white, a) * top
    table.setflags(write=False)
    return table


def table(white: int, out_bits: int) -> tuple[int, ...]:
    """The LinearizationTable: the linear value of each code."""
    a, top = _strength(white, out_bits), 2**out_bits - 1
    linear = _inverse(np.arange(top + 1, dtype=np.float64) / top, a) * white
    return tuple(int(v) for v in np.clip(np.rint(linear), 0, white))


def output_bits(sensor_bits: int, baked_bits: int, reduce: Reduce) -> int:
    """Bits stored per value: the linear values' bits (the sensor's, plus a baked vignette's) less `reduce`.
    Auto stores the sensor's bits less 2, so a baked vignette's overhead is taken back too."""
    if reduce == "auto":
        return sensor_bits - 2
    return sensor_bits + baked_bits - reduce
