from collections import deque
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from mcrawsome.pipeline.units import SlotBook, Unit, plan_units


def test_windows_are_clamped_to_a_short_clip() -> None:
    assert plan_units(3, radius=2, step=1) == [
        Unit((0, 1, 2), 0, 1),
        Unit((0, 1, 2), 1, 1),
        Unit((0, 1, 2), 2, 1),
    ]


def test_windows_slide_in_a_long_clip() -> None:
    units = plan_units(10, radius=1, step=1)
    assert units[0] == Unit((0, 1), 0, 1)
    assert units[5] == Unit((4, 5, 6), 1, 1)
    assert units[9] == Unit((8, 9), 1, 1)


def test_groups_with_a_partial_last_group() -> None:
    assert plan_units(7, radius=0, step=3) == [
        Unit((0, 1, 2), None, 3),
        Unit((3, 4, 5), None, 3),
        Unit((6,), None, 1),
    ]


def test_a_clip_shorter_than_a_group_is_one_group() -> None:
    assert plan_units(2, radius=0, step=3) == [Unit((0, 1), None, 2)]


def test_step_printing_ignores_the_temporal_radius() -> None:
    assert plan_units(6, radius=2, step=2) == plan_units(6, radius=0, step=2)


def test_sampled_indices_get_the_units_containing_them() -> None:
    assert plan_units(10, radius=1, step=1, indices=[0, 5]) == [Unit((0, 1), 0, 1), Unit((4, 5, 6), 1, 1)]
    assert plan_units(10, radius=0, step=3, indices=[1, 2, 7]) == [
        Unit((0, 1, 2), None, 3),
        Unit((6, 7, 8), None, 3),
    ]


@given(st.integers(1, 60), st.integers(0, 3), st.integers(1, 6))
def test_every_frame_is_output_exactly_once(count: int, radius: int, step: int) -> None:
    units = plan_units(count, radius, step)
    assert sum(u.repeats for u in units) == count
    for unit in units:
        assert all(0 <= f < count for f in unit.frames)
        assert list(unit.frames) == sorted(set(unit.frames))


def test_windows_in_a_span_read_frames_outside_it() -> None:
    assert plan_units(20, radius=2, step=1, span=(4, 3)) == [
        Unit((2, 3, 4, 5, 6), 2, 1),
        Unit((3, 4, 5, 6, 7), 2, 1),
        Unit((4, 5, 6, 7, 8), 2, 1),
    ]


def test_groups_in_a_span_stay_aligned_to_the_clip() -> None:
    units = plan_units(20, radius=0, step=3, span=(4, 5))
    assert units == [Unit((3, 4, 5), None, 2), Unit((6, 7, 8), None, 3)]
    assert sum(u.repeats for u in units) == 5


def test_sampled_indices_outside_the_span_are_ignored() -> None:
    assert plan_units(20, radius=1, step=1, indices=[2, 5, 9], span=(4, 3)) == [Unit((4, 5, 6), 1, 1)]
    assert plan_units(20, radius=0, step=3, indices=[1, 4], span=(4, 5)) == [Unit((3, 4, 5), None, 2)]


@given(st.data(), st.integers(1, 60), st.integers(0, 3), st.integers(1, 6))
def test_a_span_outputs_each_of_its_frames_once(
    data: st.DataObject, count: int, radius: int, step: int
) -> None:
    first = data.draw(st.integers(0, count - 1))
    length = data.draw(st.integers(1, count - first))
    units = plan_units(count, radius, step, span=(first, length))
    assert sum(u.repeats for u in units) == length
    span = range(first, first + length)
    output = [f for u in units for f in ([u.output_index] if u.centre is not None else u.frames) if f in span]
    assert output == list(span)
    for unit in units:
        assert all(0 <= f < count for f in unit.frames)


def _run(book: SlotBook, units: list[Unit], in_flight: int) -> int:
    """Process units in order as iter_frames does: acquire their frames, keep up to `in_flight` units
    pending, finish the oldest when no slot is free. Returns the most slots ever in use."""
    pending: deque[int] = deque()
    most = 0
    for position, unit in enumerate(units):
        while len(pending) >= in_flight:
            book.done(pending.popleft())
        for frame in unit.frames:
            if book.slot_of(frame) is None:
                while not book.has_free():
                    book.done(pending.popleft())
                book.acquire(frame)
        pending.append(position)
        most = max(most, book.in_use())
    while pending:
        book.done(pending.popleft())
    return most


def test_slots_are_bounded_and_released_for_sliding_windows() -> None:
    units = plan_units(100, radius=2, step=1)
    book = SlotBook(8, units)
    assert _run(book, units, in_flight=3) <= 8
    assert book.in_use() == 0


def test_a_slot_is_never_given_out_while_a_pending_unit_needs_it() -> None:
    units = plan_units(40, radius=2, step=1)
    book = SlotBook(6, units)
    pending: deque[int] = deque()
    for position, unit in enumerate(units):
        for frame in unit.frames:
            if book.slot_of(frame) is None:
                while not book.has_free():
                    book.done(pending.popleft())
                book.acquire(frame)
        slots = [book.slot_of(f) for f in unit.frames]
        assert None not in slots
        assert len(set(slots)) == len(slots)  # distinct frames in distinct slots
        pending.append(position)


def test_groups_fit_in_the_planned_slots() -> None:
    units = plan_units(50, radius=0, step=4)
    book = SlotBook(2 * 2 + 0 + 4, units)
    assert _run(book, units, in_flight=4) <= 8
    assert book.in_use() == 0


def test_acquiring_without_a_free_slot_is_an_error() -> None:
    units = plan_units(5, radius=0, step=1)
    book = SlotBook(1, units)
    book.acquire(0)
    with pytest.raises(RuntimeError, match="no free slot"):
        book.acquire(1)


def test_worker_budget_leaves_single_frame_renders_alone() -> None:
    from mcrawsome.pipeline.render import FrameJob, worker_budget

    job = FrameJob(clip_path=Path("c.mcraw"), exposure_stops=0.0, neutral=(1.0, 1.0, 1.0))
    assert worker_budget(job, (3840, 2160), 10, available=64 << 30) == (10, None)
