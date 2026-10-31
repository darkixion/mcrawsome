"""Quad Bayer remosaic, judged against a true Bayer capture of the same synthetic scenes."""

from collections.abc import Callable

import cv2
import numpy as np
import numpy.typing as npt
import pytest
from remosaic_reference import _green, _neighbours
from remosaic_reference import remosaic as reference_remosaic

from mcrawsome.camera.remosaic import bayer_colours, quad_colours, remosaic
from mcrawsome.develop.demosaic import demosaic

BLACK, WHITE, SIZE = 64.0, 4095.0, 192
Mosaic = npt.NDArray[np.uint16]
_yy, _xx = np.mgrid[0:SIZE, 0:SIZE].astype(np.float64) + 0.5


def capture(rgb: npt.NDArray[np.float64], colours: npt.NDArray[np.int8], pattern: str) -> Mosaic:
    channel = np.array(["rgb".index(c) for c in pattern])[colours]
    values = np.take_along_axis(rgb, channel[..., None], 2)[..., 0]
    return np.clip(np.rint(BLACK + values * (WHITE - BLACK)), 0, 65535).astype(np.uint16)


def pair(scene: npt.NDArray[np.float64], pattern: str) -> tuple[Mosaic, Mosaic]:
    """The scene as a Bayer sensor and as a Quad Bayer sensor of the same size record it."""
    bayer = capture(scene, bayer_colours(SIZE, SIZE), pattern)
    return bayer, capture(scene, quad_colours(pattern, SIZE, SIZE), pattern)


def pixel_swap(quad: Mosaic, pattern: str) -> Mosaic:
    """The floor: each Bayer site takes the nearest Quad Bayer sample of its colour."""
    colours = np.array(list(pattern))
    have, want = colours[quad_colours(pattern, *quad.shape)], colours[bayer_colours(*quad.shape)]
    padded, have = np.pad(quad, 4, mode="edge"), np.pad(have, 4, mode="edge")
    out, best = np.zeros_like(quad), np.full(quad.shape, 99)
    h, w = quad.shape
    for dy in range(-3, 4):
        for dx in range(-3, 4):
            here = (have[4 + dy : 4 + dy + h, 4 + dx : 4 + dx + w] == want) & (dy * dy + dx * dx < best)
            out[here] = padded[4 + dy : 4 + dy + h, 4 + dx : 4 + dx + w][here]
            best[here] = dy * dy + dx * dx
    return out


def develop(cfa: Mosaic, pattern: str) -> npt.NDArray[np.float32]:
    return demosaic((cfa.astype(np.float32) - np.float32(BLACK)) / np.float32(WHITE - BLACK), pattern)  # type: ignore[arg-type]


def psnr(a: npt.NDArray[np.floating], b: npt.NDArray[np.floating]) -> float:
    core = (slice(16, -16), slice(16, -16))
    return float(10 * np.log10(1 / np.mean((a[core].astype(np.float64) - b[core]) ** 2)))


def false_colour(rgb: npt.NDArray[np.float32]) -> float:
    core = rgb[16:-16, 16:-16]
    return float(np.mean(np.abs(core[..., 0] - core[..., 1]) + np.abs(core[..., 2] - core[..., 1])))


def grey(image: npt.NDArray[np.float64]) -> npt.NDArray[np.float64]:
    return np.repeat(image[..., None], 3, 2)


def slanted_edge() -> npt.NDArray[np.float64]:
    angle = np.deg2rad(5)
    across = (_xx - SIZE / 2) * np.cos(angle) + (_yy - SIZE / 2) * np.sin(angle)
    return grey(np.where(across > 0, 0.7, 0.1))


def colour_edges() -> npt.NDArray[np.float64]:
    colours = np.array([(0.7, 0.1, 0.1), (0.1, 0.6, 0.1), (0.1, 0.1, 0.7), (0.6, 0.6, 0.1), (0.5, 0.5, 0.5)])
    angle = np.deg2rad(7)
    u = ((_xx * np.cos(angle) + _yy * np.sin(angle)) // 37).astype(int)
    v = ((_yy * np.cos(angle) - _xx * np.sin(angle)) // 41).astype(int)
    out: npt.NDArray[np.float64] = colours[(u + 2 * v) % len(colours)]
    return out


def texture() -> npt.NDArray[np.float64]:
    rng = np.random.default_rng(1)
    out = np.zeros((SIZE, SIZE, 3))
    for scale, amount in ((32, 0.25), (8, 0.15), (4, 0.08), (2, 0.04)):
        noise = rng.random((SIZE // scale + 2, SIZE // scale + 2, 3))
        big = cv2.resize(noise, (SIZE + 2 * scale, SIZE + 2 * scale), interpolation=cv2.INTER_CUBIC)
        layer = big[scale : scale + SIZE, scale : scale + SIZE]
        out += amount * (0.7 * layer.mean(axis=2, keepdims=True) + 0.3 * layer)
    return np.clip(out, 0, 1)


def grating(frequency: float, degrees: float) -> tuple[npt.NDArray[np.float64], npt.NDArray[np.float64]]:
    angle = np.deg2rad(degrees)
    phase = 2 * np.pi * frequency * (_xx * np.cos(angle) + _yy * np.sin(angle)) + 0.3
    return grey(0.4 + 0.3 * np.cos(phase)), phase


def mtf50(method: Callable[[Mosaic, Mosaic], Mosaic], degrees: float, pattern: str) -> float:
    """The frequency (cycles per photosite) at which a grey grating keeps half its contrast."""
    previous = None
    for frequency in np.arange(0.025, 0.5, 0.025):
        scene, phase = grating(float(frequency), degrees)
        bayer, quad = pair(scene, pattern)
        lum = develop(method(bayer, quad), pattern)[32:-32, 32:-32].mean(axis=2)
        lum, phase = lum - lum.mean(), phase[32:-32, 32:-32]
        kept = float(np.hypot(np.mean(lum * np.cos(phase)), np.mean(lum * np.sin(phase))) * 2 / 0.3)
        if kept < 0.5:
            if previous is None:
                return float(frequency)
            f0, k0 = previous
            return f0 + (k0 - 0.5) / (k0 - kept) * (float(frequency) - f0)
        previous = (float(frequency), kept)
    return 0.5


PATTERNS = ["rggb", "bggr", "grbg", "gbrg"]


@pytest.mark.parametrize("pattern", PATTERNS)
def test_a_flat_colour_becomes_exactly_its_bayer_mosaic(pattern: str) -> None:
    scene = np.broadcast_to(np.array([0.6, 0.3, 0.15]), (SIZE, SIZE, 3)).copy()
    bayer, quad = pair(scene, pattern)
    assert np.abs(remosaic(quad, pattern, [BLACK] * 4).astype(int) - bayer).max() <= 1


def test_each_site_keeps_its_own_black_level() -> None:
    blacks = [60.0, 64.0, 68.0, 72.0]  # rggb: R, Gr, Gb, B
    above = np.array([800.0, 500.0, 500.0, 300.0])
    quad = (np.array(blacks) + above)[quad_colours("rggb", 16, 16)].astype(np.uint16)
    out = remosaic(quad, "rggb", blacks)
    wanted = (np.array(blacks) + above)[bayer_colours(16, 16)]
    np.testing.assert_allclose(out, wanted, atol=1)


@pytest.mark.parametrize("pattern", PATTERNS)
def test_a_neutral_edge_is_as_sharp_and_clean_as_a_bayer_capture(pattern: str) -> None:
    scene = slanted_edge()
    bayer, quad = pair(scene, pattern)
    reference = develop(bayer, pattern)
    ours, floor = (
        develop(remosaic(quad, pattern, [BLACK] * 4), pattern),
        develop(pixel_swap(quad, pattern), pattern),
    )
    assert psnr(ours, scene) > psnr(reference, scene) - 1.0
    assert psnr(ours, scene) > psnr(floor, scene) + 4.0
    assert false_colour(ours) < 1.25 * false_colour(reference) + 1e-4
    assert false_colour(ours) < false_colour(floor)


@pytest.mark.parametrize("pattern", ["rggb", "gbrg"])
@pytest.mark.parametrize("degrees", [0, 30, 45])
def test_fine_neutral_detail_is_resolved_almost_as_by_bayer(pattern: str, degrees: float) -> None:
    reference = mtf50(lambda bayer, quad: bayer, degrees, pattern)
    ours = mtf50(lambda bayer, quad: remosaic(quad, pattern, [BLACK] * 4), degrees, pattern)
    floor = mtf50(lambda bayer, quad: pixel_swap(quad, pattern), degrees, pattern)
    assert ours > 0.85 * reference
    assert floor < 0.8 * reference < ours


@pytest.mark.parametrize("scene", [colour_edges, texture], ids=["colour edges", "texture"])
def test_colour_scenes_beat_the_pixel_swap(scene: Callable[[], npt.NDArray[np.float64]]) -> None:
    truth = scene()
    bayer, quad = pair(truth, "rggb")
    ours = psnr(develop(remosaic(quad, "rggb", [BLACK] * 4), "rggb"), truth)
    floor = psnr(develop(pixel_swap(quad, "rggb"), "rggb"), truth)
    reference = psnr(develop(bayer, "rggb"), truth)
    assert ours > floor + 0.5
    assert ours < reference  # Quad Bayer samples colour more coarsely: it cannot match a Bayer sensor


def test_noise_is_not_amplified() -> None:
    rng = np.random.default_rng(7)
    bayer, quad = pair(grey(np.full((SIZE, SIZE), 0.25)), "rggb")

    def noisy(cfa: Mosaic) -> Mosaic:
        values = cfa.astype(np.float64)
        shot = rng.normal(0, 1, values.shape) * np.sqrt(np.maximum(values - BLACK, 0) + 4)
        return np.clip(np.rint(values + shot), 0, 65535).astype(np.uint16)

    reference = np.std(develop(noisy(bayer), "rggb")[16:-16, 16:-16, 1])
    ours = np.std(develop(remosaic(noisy(quad), "rggb", [BLACK] * 4), "rggb")[16:-16, 16:-16, 1])
    assert ours < 1.05 * reference


@pytest.mark.parametrize("shape", [(18, 16), (16, 6), (4, 4)])
def test_frames_must_be_whole_tiles(shape: tuple[int, int]) -> None:
    with pytest.raises(ValueError, match="whole 4x4 tiles"):
        remosaic(np.zeros(shape, np.uint16), "rggb", [0] * 4)


def test_an_unknown_pattern_is_refused() -> None:
    with pytest.raises(ValueError, match="unsupported"):
        remosaic(np.zeros((16, 16), np.uint16), "rgbw", [0] * 4)


@pytest.mark.parametrize("pattern", PATTERNS)
@pytest.mark.parametrize("shape", [(8, 8), (12, 28), (64, 96), (192, 192)])
@pytest.mark.parametrize("blacks", [[0.0] * 4, [60.25, 127.5, 31.75, 1024.0]])
def test_native_remosaic_is_pixel_exact_to_original_numpy(
    pattern: str, shape: tuple[int, int], blacks: list[float]
) -> None:
    # Includes below-black, near-saturation, arbitrary colour edges and every border phase.
    rng = np.random.default_rng(20261027)
    source = rng.integers(0, 65536, (shape[0], shape[1] * 2), dtype=np.uint16)
    cfa = source[:, ::2]  # strided input must retain the same interpretation
    expected = reference_remosaic(cfa, pattern, blacks)
    np.testing.assert_array_equal(remosaic(cfa, pattern, blacks), expected)


@pytest.mark.parametrize("pattern", PATTERNS)
def test_native_remosaic_retains_rounding_across_recorded_bit_depths(pattern: str) -> None:
    rng = np.random.default_rng(39)
    for bits in (10, 12, 16):
        for _ in range(8):
            shape = (4 * int(rng.integers(2, 10)), 4 * int(rng.integers(2, 18)))
            cfa = rng.integers(0, 1 << bits, shape, dtype=np.uint16)
            blacks: list[float] = ((rng.uniform(0, 1 << (bits - 2), 4) / 0.125).round() * 0.125).tolist()
            np.testing.assert_array_equal(
                remosaic(cfa, pattern, blacks), reference_remosaic(cfa, pattern, blacks)
            )


@pytest.mark.parametrize("greens", [{0, 3}, {1, 2}])
@pytest.mark.parametrize("shape", [(1, 1), (2, 2), (7, 13)])
def test_native_green_preserves_unrounded_float32_results(greens: set[int], shape: tuple[int, int]) -> None:
    from mcrawsome import _raw

    rng = np.random.default_rng(13)
    phases = {(y, x): rng.uniform(-1024, 65535, shape).astype(np.float32) for y in range(4) for x in range(4)}
    expected = _green(phases, _neighbours(phases), greens)
    actual = _raw.remosaic_green(list(phases.values()), sum(1 << i for i in greens))
    for phase, output in zip(phases, actual, strict=True):
        np.testing.assert_array_equal(output, expected[phase])
