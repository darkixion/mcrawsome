"""3D tables: bake a rendering to a table and apply it in NumPy.

Tables are (size**3, 3) arrays in .cube order (red varies fastest), domain 0 to 1.
"""

from __future__ import annotations

from collections.abc import Callable

import numpy as np
import numpy.typing as npt

Array = npt.NDArray[np.float64]
Table = npt.NDArray[np.float32]


def _grid(size: int) -> Array:
    g = np.linspace(0.0, 1.0, size)
    b, gg, r = np.meshgrid(g, g, g, indexing="ij")
    return np.stack([r, gg, b], axis=-1).reshape(-1, 3)


def bake(fn: Callable[[Array], Array], size: int = 65) -> Table:
    grid = _grid(size)
    out = np.concatenate([fn(grid[i : i + 50_000]) for i in range(0, len(grid), 50_000)])
    return out.astype(np.float32)


def apply_cube(table: Table, rgb: npt.ArrayLike) -> Array:
    """Tetrahedral interpolation, as FFmpeg's lut3d=interp=tetrahedral. Works in chunks to bound memory."""
    values = np.asarray(rgb, dtype=np.float64)
    flat = values.reshape(-1, 3)
    if len(flat) > _CHUNK:
        out = np.concatenate([_apply(table, flat[i : i + _CHUNK]) for i in range(0, len(flat), _CHUNK)])
        return out.reshape(values.shape)
    return _apply(table, values)


_CHUNK = 1 << 18


def _apply(table: Table, rgb: Array) -> Array:
    size = round(len(table) ** (1 / 3))
    lut = np.asarray(table, dtype=np.float64).reshape(size, size, size, 3)  # [b][g][r]
    p = np.clip(np.asarray(rgb, dtype=np.float64), 0.0, 1.0) * (size - 1)
    i0 = np.minimum(np.floor(p).astype(np.intp), size - 2)
    f = p - i0
    ir, ig, ib = i0[..., 0], i0[..., 1], i0[..., 2]
    fr, fg, fb = f[..., 0:1], f[..., 1:2], f[..., 2:3]

    def c(dr: int, dg: int, db: int) -> Array:
        return np.asarray(lut[ib + db, ig + dg, ir + dr], dtype=np.float64)

    c000, c111 = c(0, 0, 0), c(1, 1, 1)
    c100, c010, c001 = c(1, 0, 0), c(0, 1, 0), c(0, 0, 1)
    c110, c101, c011 = c(1, 1, 0), c(1, 0, 1), c(0, 1, 1)
    r_g, g_b, r_b = fr > fg, fg > fb, fr > fb
    out = np.where(
        r_g,
        np.where(
            g_b,
            c000 + fr * (c100 - c000) + fg * (c110 - c100) + fb * (c111 - c110),
            np.where(
                r_b,
                c000 + fr * (c100 - c000) + fb * (c101 - c100) + fg * (c111 - c101),
                c000 + fb * (c001 - c000) + fr * (c101 - c001) + fg * (c111 - c101),
            ),
        ),
        np.where(
            fb > fg,
            c000 + fb * (c001 - c000) + fg * (c011 - c001) + fr * (c111 - c011),
            np.where(
                fb > fr,
                c000 + fg * (c010 - c000) + fb * (c011 - c010) + fr * (c111 - c011),
                c000 + fg * (c010 - c000) + fr * (c110 - c010) + fb * (c111 - c110),
            ),
        ),
    )
    return np.asarray(out, dtype=np.float64)
