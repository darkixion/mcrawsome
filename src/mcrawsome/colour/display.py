"""The neutral display rendering: scene-referred ACEScct to SDR display code values.

The ACES 2.0 SDR output transform, as OpenColorIO 2.6's built-in studio config implements it. Previews
show RAW through it, with the clip's white balance and exposure and no creative grade.
"""

from __future__ import annotations

import functools
from typing import Any

import numpy as np
import numpy.typing as npt

from mcrawsome.colour.cube import Table, bake

Array = npt.NDArray[np.float64]

_CONFIG = "studio-config-v5.0.0_aces-v2.1_ocio-v2.6"
_DISPLAY, _VIEW = "Rec.1886 Rec.709 - Display", "ACES 2.0 - SDR 100 nits (Rec.709)"
VERSION = f"ACES 2.0 SDR output transform, OpenColorIO 2.6 built-in config {_CONFIG}"


@functools.cache
def _processor() -> Any:
    import PyOpenColorIO

    config = PyOpenColorIO.Config.CreateFromBuiltinConfig(_CONFIG)
    transform = PyOpenColorIO.DisplayViewTransform(src="ACEScct", display=_DISPLAY, view=_VIEW)
    return config.getProcessor(transform).getDefaultCPUProcessor()


def to_display(acescct: npt.ArrayLike) -> Array:
    """ACEScct (AP1, scene) to Rec.709 display code values (BT.1886, 0 to 1)."""
    v = np.asarray(acescct, dtype=np.float64)
    pixels = np.array(v.reshape(-1, 3), dtype=np.float32)  # a copy: OpenColorIO works in place
    _processor().applyRGB(pixels)
    return np.clip(np.asarray(pixels, dtype=np.float64).reshape(v.shape), 0.0, 1.0)


@functools.cache
def display_table(size: int = 65) -> Table:
    """The display rendering as a table over ACEScct, as previews apply it (see colour/cube.py)."""
    table = bake(to_display, size=size)
    table.flags.writeable = False
    return table
