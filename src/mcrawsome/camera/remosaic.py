"""Quad Bayer to Bayer remosaic: frames from a sensor whose colour filters cover 2x2 photosites each.

A Quad Bayer sensor read out at full resolution (MotionCam marks its frames `needRemosaic`) records each
colour in 2x2 cells, the cells laid out as the Bayer pattern (`rggb`: RRGG / RRGG / GGBB / GGBB). Raw
developers and editors expect one colour per photosite in the Bayer pattern, so each frame is rebuilt as
the Bayer mosaic of the same pattern and size:

1. Green everywhere: kept where it was recorded; in red and blue cells, the nearest greens to the left
   and right and above and below are interpolated, each direction weighted by how little the picture
   changes along it (the cell's own same-colour neighbours show that too), so edges are followed rather
   than blurred across.
2. Red and blue against green: the difference between each recorded red (blue) and that green is
   averaged over its cell and interpolated smoothly between cells. Green in red and blue cells is then
   the recorded red (blue) less that difference, which puts their fine detail back into green.
3. Each Bayer site takes its recorded value when the Quad Bayer site there has its colour, and otherwise
   green plus the interpolated difference.

The work is done above black (each cell's black level) and the result stored at the Bayer site's black
level, so the levels, white level, lens shading maps and colour data of the frame stay valid. The pattern
repeats every 4 photosites, so the work is done on the 16 sub-images of photosites with the same place in
their 4x4 tile, where every interpolation weight is a constant. Quantitative checks use synthetic
scenes against a true Bayer capture; an unflagged real crop has also been inspected locally.
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np
import numpy.typing as npt

from mcrawsome import _raw

_PHASES = [(y, x) for y in range(4) for x in range(4)]


def quad_site(py: int, px: int) -> int:
    """The colour filter (index into the pattern) at this place of a Quad Bayer 4x4 tile."""
    return 2 * ((py >> 1) & 1) + ((px >> 1) & 1)


def bayer_site(py: int, px: int) -> int:
    """The colour filter (index into the pattern) at this place of a Bayer mosaic."""
    return 2 * (py & 1) + (px & 1)


def quad_colours(pattern: str, height: int, width: int) -> npt.NDArray[np.int8]:
    """The colour filter (index into `pattern`) at each photosite of a Quad Bayer frame."""
    y, x = (np.arange(height) >> 1) & 1, (np.arange(width) >> 1) & 1
    return (2 * y[:, None] + x[None, :]).astype(np.int8)


def bayer_colours(height: int, width: int) -> npt.NDArray[np.int8]:
    """The colour filter (index into the pattern) at each photosite of a Bayer frame."""
    y, x = np.arange(height) & 1, np.arange(width) & 1
    return (2 * y[:, None] + x[None, :]).astype(np.int8)


def detect_quad_bayer(cfa: npt.NDArray[np.uint16], pattern: str, black_levels: Sequence[float]) -> bool:
    """Strong 4x4 colour-cell evidence in at least three quarters of a frame.

    Used only for unbinned full-sensor crops with a missing remosaic flag. A sparse grid of tiles
    averages away scene detail and noise. Its phase means must fit the recorded colours as 2x2 cells
    much better than as single Bayer sites. Flat, ambiguous and locally patterned frames abstain.
    """
    height, width = cfa.shape
    if pattern not in ("rggb", "bggr", "grbg", "gbrg") or min(height, width) < 32 or height % 4 or width % 4:
        return False
    colours = np.array(list(pattern))
    quad_sites = np.array([quad_site(y, x) for y, x in _PHASES])
    bayer_sites = np.array([bayer_site(y, x) for y, x in _PHASES])
    blacks = np.asarray(black_levels, np.float64)

    def fit(means: npt.NDArray[np.float64], sites: npt.NDArray[np.int64]) -> tuple[float, list[float]]:
        values = means - blacks[sites]
        predicted = np.empty_like(values)
        levels = []
        for colour in "rgb":
            selected = colours[sites] == colour
            level = float(values[selected].mean())
            predicted[selected] = level
            levels.append(level)
        return float(np.sqrt(np.mean((values - predicted) ** 2))), levels

    votes = 0
    rows, columns = height // 4, width // 4
    for top, bottom in ((0, rows // 2), (rows // 2, rows)):
        for left, right in ((0, columns // 2), (columns // 2, columns)):
            yy = np.linspace(top, bottom - 1, min(32, bottom - top), dtype=int) * 4
            xx = np.linspace(left, right - 1, min(32, right - left), dtype=int) * 4
            means = np.array([cfa[np.ix_(yy + y, xx + x)].mean() for y, x in _PHASES], np.float64)
            quad_error, levels = fit(means, quad_sites)
            bayer_error, _ = fit(means, bayer_sites)
            contrast = min(abs(levels[a] - levels[b]) for a, b in ((0, 1), (1, 2), (0, 2)))
            if quad_error < 0.25 * bayer_error and contrast > max(2.0, 6 * quad_error):
                votes += 1
    return votes >= 3


def remosaic(
    cfa: npt.NDArray[np.uint16], pattern: str, black_levels: Sequence[float]
) -> npt.NDArray[np.uint16]:
    """A Quad Bayer frame as the Bayer mosaic of the same `pattern` and size. `black_levels` are the four
    sites' black levels in pattern order (each Quad Bayer cell has its colour's level)."""
    if pattern not in ("rggb", "grbg", "gbrg", "bggr"):
        raise ValueError(f"unsupported Bayer pattern: {pattern}")
    height, width = cfa.shape
    if height % 4 or width % 4 or height < 8 or width < 8:
        raise ValueError(
            f"a Quad Bayer frame needs a size of whole 4x4 tiles, at least 8x8, not {width}x{height}"
        )
    return _raw.remosaic(cfa, pattern, np.asarray(black_levels, dtype=np.float32))
