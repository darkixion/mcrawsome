"""Exact Bayer-site averaging, including the original tie-to-even and edge-crop rules."""

import numpy as np
import numpy.typing as npt
import pytest

from mcrawsome import _raw


def reference(cfa: npt.NDArray[np.uint16], factor: int) -> npt.NDArray[np.uint16]:
    block = 2 * factor
    height, width = (n // block * block for n in cfa.shape)
    out = np.empty((height // factor, width // factor), np.uint16)
    for py in range(2):
        for px in range(2):
            total = np.zeros((height // block, width // block), np.uint32)
            for dy in range(0, block, 2):
                for dx in range(0, block, 2):
                    total += cfa[py + dy : height : block, px + dx : width : block]
            out[py::2, px::2] = np.rint(total / factor**2).astype(np.uint16)
    return out


@pytest.mark.parametrize("factor", [2, 4, 8])
@pytest.mark.parametrize("extra", [0, 1, 7])
def test_native_cfa_reduction_matches_original_with_strides_and_cropped_edges(
    factor: int, extra: int
) -> None:
    rng = np.random.default_rng(40)
    source = rng.integers(0, 65536, (factor * 6 + extra, (factor * 10 + extra) * 2), dtype=np.uint16)
    cfa = source[:, ::2]
    cfa.flags.writeable = False
    np.testing.assert_array_equal(_raw.reduce_cfa(cfa, factor), reference(cfa, factor))


@pytest.mark.parametrize("factor", [2, 4, 8])
@pytest.mark.parametrize("base", [0, 2, 65531])
def test_native_cfa_reduction_rounds_each_site_halfway_to_even(factor: int, base: int) -> None:
    source = np.empty((factor * 2, factor * 2), np.uint16)
    wanted = np.empty((2, 2), np.uint16)
    for py in range(2):
        for px in range(2):
            value = base + 2 * py + px
            source[py::2, px::2] = value
            source[py::2, px::2].flat[: factor**2 // 2] += 1
            wanted[py, px] = value + (value & 1)
    np.testing.assert_array_equal(_raw.reduce_cfa(source, factor), wanted)


@pytest.mark.parametrize("factor", [2, 4, 8])
def test_native_cfa_reduction_does_not_overflow_at_white(factor: int) -> None:
    source = np.full((factor * 4, factor * 6), 65535, np.uint16)
    np.testing.assert_array_equal(_raw.reduce_cfa(source, factor), np.full((4, 6), 65535, np.uint16))


@pytest.mark.parametrize("factor", [0, 1, 3, 16])
def test_native_cfa_reduction_rejects_unsupported_factors(factor: int) -> None:
    with pytest.raises(ValueError, match="factors"):
        _raw.reduce_cfa(np.zeros((32, 32), np.uint16), factor)


@pytest.mark.parametrize("shape", [(8, 8), (8,), (16, 16, 1)])
def test_native_cfa_reduction_rejects_invalid_dimensions(shape: tuple[int, ...]) -> None:
    with pytest.raises(ValueError, match="small"):
        _raw.reduce_cfa(np.zeros(shape, np.uint16), 8)
