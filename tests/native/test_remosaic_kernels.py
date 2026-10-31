"""Native interpolation boundaries and float32 arithmetic before final RAW rounding."""

import numpy as np
import pytest

from mcrawsome import _raw


@pytest.mark.parametrize("shape", [(4, 4), (7, 8), (8, 10), (8,), (8, 8, 1)])
def test_full_remosaic_rejects_invalid_shape(shape: tuple[int, ...]) -> None:
    with pytest.raises(ValueError, match="4x4 tiles"):
        _raw.remosaic(np.zeros(shape, np.uint16), "rggb", np.zeros(4, np.float32))


@pytest.mark.parametrize("shape", [(3,), (2, 2), (5,)])
def test_full_remosaic_rejects_invalid_black_sites(shape: tuple[int, ...]) -> None:
    with pytest.raises(ValueError, match="four sites"):
        _raw.remosaic(np.zeros((8, 8), np.uint16), "rggb", np.zeros(shape, np.float32))


def test_full_remosaic_rejects_invalid_pattern() -> None:
    with pytest.raises(ValueError, match="unsupported"):
        _raw.remosaic(np.zeros((8, 8), np.uint16), "rgbw", np.zeros(4, np.float32))


def test_full_remosaic_preserves_readonly_input() -> None:
    source = np.arange(8 * 12, dtype=np.uint16).reshape(8, 12)
    expected = source.copy()
    source.flags.writeable = False
    output = _raw.remosaic(source, "rggb", np.array([60.25, 31.5, 72.75, 64.0], np.float32))
    assert output.shape == source.shape
    assert output.dtype == np.uint16
    np.testing.assert_array_equal(source, expected)


@pytest.mark.parametrize("ty", [-1, 0])
@pytest.mark.parametrize("tx", [-1, 0])
@pytest.mark.parametrize(("fy", "fx"), [(0.125, 0.875), (0.375, 0.625), (0.0, 1.0)])
def test_difference_matches_float32_array_operations(ty: int, tx: int, fy: float, fx: float) -> None:
    rng = np.random.default_rng(42)
    grid, guide = (rng.uniform(-1024, 65535, (19, 31)).astype(np.float32) for _ in range(2))
    here = rng.uniform(-1024, 65535, (17, 29)).astype(np.float32)
    total, weights = np.zeros_like(here), np.zeros_like(here)
    for dy, wy in ((ty, 1 - fy), (ty + 1, fy)):
        for dx, wx in ((tx, 1 - fx), (tx + 1, fx)):
            if wy * wx == 0:
                continue
            window = (slice(1 + dy, 18 + dy), slice(1 + dx, 30 + dx))
            near = guide[window]
            spread = np.float32(0.05) * (np.abs(near) + np.abs(here)) + np.float32(8)
            weight = np.float32(wy * wx) / (np.float32(1) + np.square((near - here) / spread))
            total += grid[window] * weight
            weights += weight
    np.testing.assert_array_equal(
        _raw.remosaic_difference(grid, guide, here, ty, tx, fy, fx), total / weights
    )


@pytest.mark.parametrize("shape", [(0, 2), (2, 0), (2,), (2, 2, 1)])
def test_difference_rejects_invalid_dimensions(shape: tuple[int, ...]) -> None:
    with pytest.raises(ValueError, match="border"):
        _raw.remosaic_difference(
            np.zeros((4, 4), np.float32),
            np.zeros((4, 4), np.float32),
            np.zeros(shape, np.float32),
            0,
            0,
            0.5,
            0.5,
        )


@pytest.mark.parametrize(
    ("ty", "tx", "fy", "fx"),
    [(-2, 0, 0.5, 0.5), (0, 1, 0.5, 0.5), (0, 0, -0.1, 0.5), (0, 0, 0.5, float("nan"))],
)
def test_difference_rejects_invalid_corners(ty: int, tx: int, fy: float, fx: float) -> None:
    with pytest.raises(ValueError, match="range"):
        _raw.remosaic_difference(
            np.zeros((4, 4), np.float32),
            np.zeros((4, 4), np.float32),
            np.zeros((2, 2), np.float32),
            ty,
            tx,
            fy,
            fx,
        )


def test_green_rejects_mismatched_phase_shapes() -> None:
    phases = [np.zeros((2, 2), np.float32) for _ in range(16)]
    phases[-1] = np.zeros((1, 2), np.float32)
    with pytest.raises(ValueError, match="matching"):
        _raw.remosaic_green(phases, 6)


@pytest.mark.parametrize(("count", "mask"), [(15, 6), (16, 3), (16, 0)])
def test_green_rejects_invalid_phase_inventory(count: int, mask: int) -> None:
    with pytest.raises(ValueError, match="sixteen"):
        _raw.remosaic_green([np.zeros((2, 2), np.float32) for _ in range(count)], mask)


def test_green_accepts_readonly_phases_without_modifying_them() -> None:
    phases = [np.full((3, 7), float(i), np.float32) for i in range(16)]
    for phase in phases:
        phase.flags.writeable = False
    outputs = _raw.remosaic_green(phases, 6)
    assert len(outputs) == 16
    for i, phase in enumerate(phases):
        np.testing.assert_array_equal(phase, np.full((3, 7), float(i), np.float32))


def test_difference_rejects_mismatched_guide() -> None:
    with pytest.raises(ValueError, match="border"):
        _raw.remosaic_difference(
            np.zeros((4, 4), np.float32),
            np.zeros((4, 5), np.float32),
            np.zeros((2, 2), np.float32),
            0,
            0,
            0.5,
            0.5,
        )
