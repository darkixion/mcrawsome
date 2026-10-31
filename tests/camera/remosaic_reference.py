"""Frozen NumPy remosaic oracle from 90475a9, before native acceleration.

Keep its operation order independent of the production kernels for exact-pixel regressions.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

import numpy as np
import numpy.typing as npt

Float = npt.NDArray[np.float32]
Phase = tuple[int, int]
_EPS = np.float32(1.0)
SPREAD, FLOOR = 0.1, 8.0  # how unlike green may be (a share of it, plus counts) before a cell counts less
_PHASES = [(y, x) for y in range(4) for x in range(4)]


def quad_site(py: int, px: int) -> int:
    """The colour filter (index into the pattern) at this place of a Quad Bayer 4x4 tile."""
    return 2 * ((py >> 1) & 1) + ((px >> 1) & 1)


def bayer_site(py: int, px: int) -> int:
    """The colour filter (index into the pattern) at this place of a Bayer mosaic."""
    return 2 * (py & 1) + (px & 1)


def _neighbours(phases: dict[Phase, Float]) -> Callable[[int, int, int, int], Float]:
    """`at(py, px, dy, dx)`: for the photosites at (py, px) of each tile, the value `dy` rows and `dx`
    columns away (|dy|, |dx| at most 4), the frame's edge repeated."""
    padded = {phase: np.pad(values, 1, mode="edge") for phase, values in phases.items()}
    height, width = next(iter(phases.values())).shape

    def at(py: int, px: int, dy: int, dx: int) -> Float:
        y, x = py + dy, px + dx
        sy, sx = y // 4, x // 4
        view: Float = padded[(y % 4, x % 4)][1 + sy : 1 + sy + height, 1 + sx : 1 + sx + width]
        return view

    return at


def _green(
    phases: dict[Phase, Float], at: Callable[[int, int, int, int], Float], greens: set[int]
) -> dict[Phase, Float]:
    """Green at every photosite: recorded, or interpolated along the direction that changes least."""
    green = {}
    for (py, px), values in phases.items():
        if quad_site(py, px) in greens:
            green[(py, px)] = values
            continue
        total, weights = np.zeros_like(values), np.zeros_like(values)
        for vertical in (False, True):
            offset = (py if vertical else px) & 1
            # Offset 0 in its cell: greens 1 and 2 before, 2 and 3 after, its sibling after; offset 1: greens
            # 2 and 3 before, 1 and 2 after, its sibling before.
            steps = (-1, -2, 2, 3, 1) if offset == 0 else (-2, -3, 1, 2, -1)
            near_before, far_before, near_after, far_after, sibling = (
                at(py, px, d, 0) if vertical else at(py, px, 0, d) for d in steps
            )
            before = np.float32(2 / 3 if offset == 0 else 1 / 3)
            value = near_before * before + near_after * (1 - before)
            change = (
                np.abs(near_before - near_after)
                + np.float32(0.5) * (np.abs(far_before - near_before) + np.abs(far_after - near_after))
                + np.float32(2) * np.abs(values - sibling)  # the same colour beside it in its cell
            )
            weight = np.float32(1) / (change + _EPS)
            total += value * weight
            weights += weight
        green[(py, px)] = total / weights
    return green


def _corners(place: int, centre: float) -> tuple[int, float]:
    """The tile offset of the cell centre before this place, and how far the place is towards the next."""
    position = (place - centre) / 4
    before = int(np.floor(position))
    return before, position - before


def _differences(phases: dict[Phase, Float], green: dict[Phase, Float], site: int) -> dict[Phase, Float]:
    """Recorded colour less green, averaged over each cell of colour `site` and interpolated to every
    photosite between the four nearest cells, each weighted by its distance and by how alike its green is
    to the photosite's: a value comes from the side of an edge the photosite is on."""
    a, b = divmod(site, 2)
    cells = [(2 * a + i, 2 * b + j) for i in (0, 1) for j in (0, 1)]
    quarter = np.float32(0.25)
    grid = np.pad(sum((phases[c] - green[c] for c in cells), np.float32(0)) * quarter, 1, mode="edge")
    guide = np.pad(sum((green[c] for c in cells), np.float32(0)) * quarter, 1, mode="edge")
    height, width = grid.shape[0] - 2, grid.shape[1] - 2
    out = {}
    for py, px in _PHASES:
        here = green[(py, px)]
        (ty, fy), (tx, fx) = _corners(py, 2 * a + 0.5), _corners(px, 2 * b + 0.5)
        total, weights = np.zeros_like(here), np.zeros_like(here)
        for dy, wy in ((ty, 1 - fy), (ty + 1, fy)):
            for dx, wx in ((tx, 1 - fx), (tx + 1, fx)):
                if wy * wx == 0:
                    continue
                window = (slice(1 + dy, 1 + dy + height), slice(1 + dx, 1 + dx + width))
                near = guide[window]
                spread = np.float32(SPREAD / 2) * (np.abs(near) + np.abs(here)) + np.float32(FLOOR)
                weight = np.float32(wy * wx) / (np.float32(1) + np.square((near - here) / spread))
                total += grid[window] * weight
                weights += weight
        out[(py, px)] = total / weights
    return out


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
    blacks = [np.float32(level) for level in black_levels]
    phases = {
        (py, px): cfa[py::4, px::4].astype(np.float32) - blacks[quad_site(py, px)] for py, px in _PHASES
    }
    greens = {i for i, colour in enumerate(pattern) if colour == "g"}
    green = _green(phases, _neighbours(phases), greens)
    difference = {colour: _differences(phases, green, pattern.index(colour)) for colour in "rb"}
    refined = dict(green)
    for colour in "rb":
        for phase in _PHASES:
            if quad_site(*phase) == pattern.index(colour):
                refined[phase] = phases[phase] - difference[colour][phase]
    out = np.empty_like(cfa)
    for phase in _PHASES:
        recorded, wanted = quad_site(*phase), bayer_site(*phase)
        if pattern[recorded] == pattern[wanted]:
            value = phases[phase]
        elif pattern[wanted] == "g":
            value = refined[phase]
        else:
            value = refined[phase] + difference[pattern[wanted]][phase]
        out[phase[0] :: 4, phase[1] :: 4] = np.clip(np.rint(value + blacks[wanted]), 0, 65535)
    return out
