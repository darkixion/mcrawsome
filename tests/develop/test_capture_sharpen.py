"""Video capture sharpening restores coherent detail without boosting flat-field noise."""

import cv2
import numpy as np
import pytest

from mcrawsome.develop.noise import NoiseModel
from mcrawsome.develop.sharpen import capture_sharpen


def edge() -> np.ndarray:
    image = np.full((96, 160), 0.05, np.float32)
    image[:, 80:] = 0.7
    return np.repeat(cv2.GaussianBlur(image, (0, 0), 1.1)[..., None], 3, -1)


def test_soft_edge_gets_steeper_without_new_extrema_or_colour_differences() -> None:
    image = edge() + np.array([0.02, 0.0, -0.01], np.float32)
    before = image.copy()
    output = capture_sharpen(image, NoiseModel(0.00001, 0.0000001), 1, 0.8)
    assert output[30, 80, 1] - output[30, 79, 1] > 1.1 * (image[30, 80, 1] - image[30, 79, 1])
    assert output[..., 1].min() >= image[..., 1].min() - 1e-7
    assert output[..., 1].max() <= image[..., 1].max() + 1e-7
    np.testing.assert_allclose(output[..., 0] - output[..., 1], image[..., 0] - image[..., 1], atol=1e-7)
    np.testing.assert_array_equal(image, before)
    assert output.dtype == np.float32


def test_recorded_noise_keeps_a_flat_shadow_quiet() -> None:
    rng = np.random.default_rng(83)
    y = rng.normal(0.025, 0.01, (256, 256)).astype(np.float32)
    image = np.repeat(y[..., None], 3, -1)
    output = capture_sharpen(image, NoiseModel(0, 0.0001), 1, 1.2)
    assert output[..., 0].std() < 1.03 * y.std()
    assert abs(float(output[..., 0].mean() - y.mean())) < 1e-5


def test_accepted_temporal_neighbours_allow_more_detail() -> None:
    image = edge()
    model = NoiseModel(0, 0.0004)
    single = capture_sharpen(image, model, 1, 0.8)
    averaged = capture_sharpen(image, model, 1, 0.8, effective_frames=9)
    assert np.abs(averaged - image).mean() > 1.5 * np.abs(single - image).mean()


@pytest.mark.parametrize("model", [None, NoiseModel(-1, 0), NoiseModel(float("nan"), 0), NoiseModel(0, 0)])
def test_missing_or_invalid_noise_does_not_guess(model: NoiseModel | None) -> None:
    image = edge()
    assert capture_sharpen(image, model, 1, 0.8) is image


@pytest.mark.parametrize("level", [-0.01, 0.0, 4.0])
@pytest.mark.parametrize("shape", [(1, 1), (2, 5), (20, 30)])
def test_constant_negative_and_hdr_frames_stay_unchanged(level: float, shape: tuple[int, int]) -> None:
    image = np.full((*shape, 3), level, np.float32)
    np.testing.assert_allclose(capture_sharpen(image, NoiseModel(0.001, 0.00001), 1, 0.8), image, atol=1e-6)


def test_off_is_exact_and_sharpening_scales_with_exposure() -> None:
    image = edge()
    model = NoiseModel(0.001, 0.00001)
    assert capture_sharpen(image, model, 0, 0.8) is image
    regular = capture_sharpen(image, model, 0.7, 0.8)
    bright = capture_sharpen(image * 2, NoiseModel(0.002, 0.00004), 0.7, 0.8)
    np.testing.assert_allclose(bright, regular * 2, atol=1e-6)
