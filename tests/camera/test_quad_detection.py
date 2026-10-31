"""A missing Quad Bayer flag is recovered only with strong, consistent photosite evidence."""

import numpy as np
import numpy.typing as npt
import pytest

from mcrawsome.camera.remosaic import bayer_colours, detect_quad_bayer, quad_colours

Mosaic = npt.NDArray[np.uint16]


def scene(pattern: str, quad: bool, *, noise: float = 2.0) -> Mosaic:
    y, x = np.mgrid[:128, :192]
    grey = 0.8 + 0.1 * np.sin(x / 11) + 0.1 * np.cos(y / 17)
    rgb = grey[..., None] * np.array([0.35, 0.6, 0.17])
    sites = quad_colours(pattern, 128, 192) if quad else bayer_colours(128, 192)
    colours = np.array(["rgb".index(c) for c in pattern])[sites]
    values = np.take_along_axis(rgb, colours[..., None], 2)[..., 0]
    values = 64 + 900 * values + np.random.default_rng(7).normal(0, noise, values.shape)
    return np.rint(values).astype(np.uint16)


@pytest.mark.parametrize("pattern", ["rggb", "bggr", "grbg", "gbrg"])
@pytest.mark.parametrize("noise", [0, 2, 12])
def test_recognises_quad_cells_but_keeps_ordinary_bayer(pattern: str, noise: float) -> None:
    assert detect_quad_bayer(scene(pattern, True, noise=noise), pattern, [64] * 4)
    assert not detect_quad_bayer(scene(pattern, False, noise=noise), pattern, [64] * 4)


@pytest.mark.parametrize("value", [0, 64, 1023, 65535])
def test_a_flat_frame_without_colour_evidence_is_not_reinterpreted(value: int) -> None:
    assert not detect_quad_bayer(np.full((64, 128), value, np.uint16), "gbrg", [64] * 4)


def test_independent_random_noise_is_not_a_quad_pattern() -> None:
    raw = np.random.default_rng(3).integers(64, 500, (128, 192), dtype=np.uint16)
    assert not detect_quad_bayer(raw, "gbrg", [64] * 4)


def test_a_single_coloured_patch_does_not_decide_the_whole_frame() -> None:
    raw = np.full((128, 192), 64, np.uint16)
    raw[:64, :96] = scene("gbrg", True)[:64, :96]
    assert not detect_quad_bayer(raw, "gbrg", [64] * 4)


def test_achromatic_two_pixel_checks_do_not_establish_a_colour_filter_layout() -> None:
    y, x = np.mgrid[:128, :192]
    raw = (100 + 200 * (((x // 2) + (y // 2)) % 2)).astype(np.uint16)
    assert not detect_quad_bayer(raw, "gbrg", [64] * 4)
