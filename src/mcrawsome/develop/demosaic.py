"""Malvar-He-Cutler demosaic for any 2x2 Bayer layout."""

from __future__ import annotations

from typing import Literal

import cv2
import numpy as np
import numpy.typing as npt

Image = npt.NDArray[np.float32]
Layout = Literal["rggb", "bggr", "grbg", "gbrg"]


def _k(rows: list[list[float]]) -> Image:
    return np.array(rows, dtype=np.float32) / 8.0


_G_AT_RB = _k([[0, 0, -1, 0, 0], [0, 0, 2, 0, 0], [-1, 2, 4, 2, -1], [0, 0, 2, 0, 0], [0, 0, -1, 0, 0]])
_RB_AT_G_ROW = _k(
    [[0, 0, 0.5, 0, 0], [0, -1, 0, -1, 0], [-1, 4, 5, 4, -1], [0, -1, 0, -1, 0], [0, 0, 0.5, 0, 0]]
)
_RB_AT_G_COL = np.ascontiguousarray(_RB_AT_G_ROW.T)
_RB_AT_BR = _k(
    [[0, 0, -1.5, 0, 0], [0, 2, 0, 2, 0], [-1.5, 0, 6, 0, -1.5], [0, 2, 0, 2, 0], [0, 0, -1.5, 0, 0]]
)


def _demosaic_rggb(m: Image) -> Image:
    def conv(kernel: Image) -> Image:
        return np.asarray(cv2.filter2D(m, -1, kernel, borderType=cv2.BORDER_REFLECT), dtype=np.float32)

    g_rb, rb_grow, rb_gcol, rb_br = conv(_G_AT_RB), conv(_RB_AT_G_ROW), conv(_RB_AT_G_COL), conv(_RB_AT_BR)
    r, g, b = np.empty_like(m), m.copy(), np.empty_like(m)
    r[0::2, 0::2], g[0::2, 0::2], b[0::2, 0::2] = m[0::2, 0::2], g_rb[0::2, 0::2], rb_br[0::2, 0::2]
    b[1::2, 1::2], g[1::2, 1::2], r[1::2, 1::2] = m[1::2, 1::2], g_rb[1::2, 1::2], rb_br[1::2, 1::2]
    r[0::2, 1::2], b[0::2, 1::2] = rb_grow[0::2, 1::2], rb_gcol[0::2, 1::2]
    b[1::2, 0::2], r[1::2, 0::2] = rb_grow[1::2, 0::2], rb_gcol[1::2, 0::2]
    return np.dstack([r, g, b])


def demosaic(mosaic: Image, cfa: Layout) -> Image:
    """Return an (h, w, 3) RGB image. Other layouts are shifted onto RGGB by mirror padding."""
    dy, dx = divmod(cfa.index("r"), 2)
    if dy == 0 and dx == 0:
        return _demosaic_rggb(np.ascontiguousarray(mosaic, dtype=np.float32))
    padded = np.pad(mosaic.astype(np.float32), ((dy, dy), (dx, dx)), mode="reflect")
    h, w = mosaic.shape
    return np.ascontiguousarray(_demosaic_rggb(padded)[dy : dy + h, dx : dx + w])
