import numpy as np
import pytest

from mcrawsome.analysis.measure import combine, frame_stats, mired_spread


def _grey(level: float, sigma: float = 0.0, seed: int = 3) -> np.ndarray:
    rng = np.random.default_rng(seed)
    image = np.full((1080, 1920, 3), level, np.float32)
    if sigma:
        image += rng.normal(0, sigma, (1080, 1920, 1)).astype(np.float32)  # luminance noise
    return image


def test_a_uniform_grey_has_its_level_as_key_and_no_noise() -> None:
    stats = combine([frame_stats(_grey(0.09), clip_level=None)])
    assert stats.key == pytest.approx(0.09, rel=0.02)
    assert stats.noise_rel < 1e-3
    assert stats.clipped_share == 0.0


def test_noise_of_a_known_size_is_measured() -> None:
    stats = combine([frame_stats(_grey(0.09, sigma=0.005), clip_level=None)])
    assert stats.noise_rel == pytest.approx(0.005 / 0.09, rel=0.15)
    assert stats.noise_level == pytest.approx(0.09, rel=0.05)


def test_noise_at_mid_grey_grows_with_the_exposure_push() -> None:
    stats = combine([frame_stats(_grey(0.09, sigma=0.005), clip_level=None)])
    # a push of 1 stop makes the 0.09 pixels mid-grey: noise relative to them is unchanged
    assert stats.noise_mid_grey(1.0) == pytest.approx(stats.noise_rel, rel=0.01)
    # a push of 3 stops makes darker (noisier, shot-noise-limited) pixels mid-grey
    assert stats.noise_mid_grey(3.0) == pytest.approx(stats.noise_rel * 2.0, rel=0.01)


def test_clipped_pixels_are_counted_for_video_inputs() -> None:
    image = _grey(0.2)
    image[:108] = 5.0  # a tenth of the frame at the encoding's top
    stats = combine([frame_stats(image, clip_level=4.9)])
    assert stats.clipped_share == pytest.approx(0.1, abs=0.01)


def test_the_key_ignores_the_brightest_two_percent() -> None:
    image = _grey(0.09)
    image[:10] = 50.0  # a lamp
    assert combine([frame_stats(image, clip_level=None)]).key == pytest.approx(0.09, rel=0.02)


def test_mired_spread() -> None:
    assert mired_spread([5000.0, 5000.0, 5000.0]) == 0.0
    assert mired_spread([5000.0, 5250.0, 5500.0]) == pytest.approx(1e6 / 5000 - 1e6 / 5500, abs=0.01)
