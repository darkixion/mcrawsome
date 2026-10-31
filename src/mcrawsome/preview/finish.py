"""The preview's cheap end: exposure and the display table on a kept frame, fast enough for a slider.

The kept frame is scene-linear, so a new exposure only scales it before the ACEScct encoding; the last
exposure's encoded frame is kept, and the display table is applied by the native extension straight to
8 bits. The result matches `finish_frame` and the display table to within rounding; exports never use
this path.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import numpy.typing as npt

from mcrawsome import _raw
from mcrawsome.colour.cube import Table
from mcrawsome.colour.transfer import acescct_encode32

Image = npt.NDArray[np.float32]
_BANDS = max(1, min(8, os.cpu_count() or 1))
_pool: ThreadPoolExecutor | None = None


def _in_bands(work: Callable[[slice], None], rows: int) -> None:
    """`work` on bands of rows at once, on threads (NumPy lets go of the GIL while it works)."""
    global _pool
    if _pool is None:
        _pool = ThreadPoolExecutor(_BANDS, thread_name_prefix="preview-band")
    step = -(-rows // _BANDS)
    for done in [_pool.submit(work, slice(top, top + step)) for top in range(0, rows, step)]:
        done.result()


class Finisher:
    """One kept frame (scene-linear AP1 at preview size, recorded orientation) ready to be finished at any
    exposure, turned for display by `rotation`."""

    def __init__(self, linear: Image, rotation: int) -> None:
        if rotation:
            linear = np.ascontiguousarray(np.rot90(linear, -rotation // 90))
        height, width = linear.shape[:2]
        self.size = (width, height)
        self._base = linear
        self._last: tuple[float, Image] | None = None

    def encoded(self, stops: float) -> Image:
        """The frame at `stops` as ACEScct (not clipped); the last one is kept."""
        if self._last is not None and self._last[0] == stops:
            return self._last[1]
        gain = np.float32(2.0**stops)
        encoded = np.empty_like(self._base)

        def lit(rows: slice) -> None:
            encoded[rows] = acescct_encode32(self._base[rows] * gain)

        _in_bands(lit, encoded.shape[0])
        self._last = (stops, encoded)
        return encoded


def display_bgr(encoded: Image, table: Table) -> npt.NDArray[np.uint8]:
    """ACEScct through the display table (clipped to 0 to 1 first), as 8-bit BGR for OpenCV."""
    return _raw.apply_look(np.ascontiguousarray(encoded, dtype=np.float32), table, bgr=True)


def codes16(encoded: Image) -> npt.NDArray[np.uint16]:
    """ACEScct as finish_frame gives it: clipped and scaled to 16 bits."""
    return np.asarray(np.clip(encoded, 0.0, 1.0) * 65535.0 + 0.5, dtype=np.uint16)
