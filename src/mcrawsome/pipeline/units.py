"""Work units: the source frames that make one output frame (a temporal window) or several (a
step-printing group), and the bookkeeping of shared-memory slots for decoded video frames."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class Unit:
    """`frames` in order. A window has `centre`, the position of its output frame in `frames`, and
    one repeat; a step group has no centre and is printed once per source frame."""

    frames: tuple[int, ...]
    centre: int | None
    repeats: int
    output: int | None = None  # the number of the unit's first output frame, when it differs from its source

    @property
    def output_index(self) -> int:
        """The source frame whose time the output frame takes (for stabilisation)."""
        return self.frames[self.centre if self.centre is not None else 0]

    @property
    def first_output(self) -> int:
        """The number of the unit's first output frame (for grain and other per-frame effects)."""
        return self.output if self.output is not None else self.output_index


def plan_units(
    frame_count: int,
    radius: int,
    step: int,
    indices: Sequence[int] | None = None,
    span: tuple[int, int] | None = None,
    sources: Sequence[int] | None = None,
) -> list[Unit]:
    """Units of a clip of `frame_count` source frames, in order. Windows are clamped to the clip and
    step groups start at multiples of `step` from frame 0, so a frame's unit is the same whatever range
    is rendered. `span = (first, count)` keeps the units with an output frame in that range (the whole
    clip by default), and a group's `repeats` counts only its frames inside it. `indices` (source
    frames) keeps just the units containing them, each once; indices outside the span are ignored.
    Step printing (`step` > 1) ignores `radius`: averaging the group already reduces noise.

    With `sources` (the source frame of each output frame, when dropped frames are filled),
    `frame_count`, `span` and `indices` count output frames and the units carry their output numbers.
    Windows take source neighbours, and a repeated frame joins the unit of the frame before it."""
    if sources is not None:
        return _plan_mapped(sources, radius, step, indices, span)
    first, count = span if span is not None else (0, frame_count)
    stop = min(first + count, frame_count)
    first = max(first, 0)
    wanted = range(first, stop) if indices is None else sorted({i for i in indices if first <= i < stop})
    if step > 1:
        units = []
        for start in sorted({i // step * step for i in wanted}):
            group = tuple(range(start, min(start + step, frame_count)))
            units.append(Unit(group, None, min(start + step, stop) - max(start, first)))
        return units
    units = []
    for centre in wanted:
        low, high = max(0, centre - radius), min(frame_count, centre + radius + 1)
        units.append(Unit(tuple(range(low, high)), centre - low, 1))
    return units


def _plan_mapped(
    sources: Sequence[int],
    radius: int,
    step: int,
    indices: Sequence[int] | None,
    span: tuple[int, int] | None,
) -> list[Unit]:
    """plan_units on output frames. A step group exposes the distinct source frames of its output frames.
    Outside sampling, a run of output frames showing one source frame is one unit, made once."""
    total, source_count = len(sources), sources[-1] + 1
    first, count = span if span is not None else (0, total)
    stop = min(first + count, total)
    first = max(first, 0)
    wanted = range(first, stop) if indices is None else sorted({i for i in indices if first <= i < stop})
    units: list[Unit] = []
    if step > 1:
        for start in sorted({i // step * step for i in wanted}):
            group = tuple(dict.fromkeys(sources[start : min(start + step, total)]))
            units.append(Unit(group, None, min(start + step, stop) - max(start, first), start))
        return units
    for output in wanted:
        centre = sources[output]
        if indices is None and units and units[-1].output_index == centre:
            last = units[-1]
            units[-1] = Unit(last.frames, last.centre, last.repeats + 1, last.output)
            continue
        low, high = max(0, centre - radius), min(source_count, centre + radius + 1)
        units.append(Unit(tuple(range(low, high)), centre - low, 1, output))
    return units


class SlotBook:
    """Which ring slot holds which decoded frame. A frame keeps its slot until the last unit that
    needs it is done, so no slot is reused while a submitted unit still reads it."""

    def __init__(self, slots: int, units: Sequence[Unit]) -> None:
        self._free = list(range(slots - 1, -1, -1))
        self._slot: dict[int, int] = {}
        self._last_use: dict[int, int] = {}
        for position, unit in enumerate(units):
            for frame in unit.frames:
                self._last_use[frame] = position

    def slot_of(self, frame: int) -> int | None:
        return self._slot.get(frame)

    def has_free(self) -> bool:
        return bool(self._free)

    def in_use(self) -> int:
        return len(self._slot)

    def acquire(self, frame: int) -> int:
        if not self._free:
            raise RuntimeError("no free slot: finish a pending unit first")
        slot = self._free.pop()
        self._slot[frame] = slot
        return slot

    def done(self, position: int) -> None:
        """Unit `position` (and every earlier one) is finished: free the frames no later unit needs."""
        for frame in [f for f in self._slot if self._last_use[f] <= position]:  # at most `slots` frames
            self._free.append(self._slot.pop(frame))
