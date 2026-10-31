import numpy as np
import pytest

from mcrawsome.develop.demosaic import demosaic

LAYOUTS = ["rggb", "bggr", "grbg", "gbrg"]
SITE = {"r": 0, "g": 1, "b": 2}


def _mosaic(cfa: str, rgb: tuple[float, float, float], h: int = 16, w: int = 16) -> np.ndarray:
    m = np.zeros((h, w), np.float32)
    for i, colour in enumerate(cfa):
        y, x = divmod(i, 2)
        m[y::2, x::2] = rgb[SITE[colour]]
    return m


@pytest.mark.parametrize("cfa", LAYOUTS)
def test_uniform_colour_is_reconstructed_everywhere(cfa: str) -> None:
    out = demosaic(_mosaic(cfa, (0.2, 0.5, 0.8)), cfa)  # type: ignore[arg-type]
    assert out.shape == (16, 16, 3)
    np.testing.assert_allclose(out[4:-4, 4:-4], np.broadcast_to([0.2, 0.5, 0.8], (8, 8, 3)), atol=1e-5)


@pytest.mark.parametrize("cfa", LAYOUTS)
def test_known_sites_are_kept(cfa: str) -> None:
    rng = np.random.default_rng(0)
    m = rng.random((16, 16), dtype=np.float32)
    out = demosaic(m, cfa)  # type: ignore[arg-type]
    for i, colour in enumerate(cfa):
        y, x = divmod(i, 2)
        np.testing.assert_allclose(out[y::2, x::2, SITE[colour]], m[y::2, x::2], atol=1e-6)
