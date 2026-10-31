import numpy as np
import pytest

from mcrawsome.develop.highlights import recover_highlights

CLIP = 1.0


def _orange(height: int = 64, width: int = 128) -> tuple[np.ndarray, np.ndarray]:
    """A smooth orange gradient whose red channel rises past sensor white in the middle."""
    x = np.linspace(0.0, 1.0, width, dtype=np.float32)
    y = np.linspace(0.8, 1.2, height, dtype=np.float32)[:, None]
    level = (0.3 + 1.2 * np.exp(-(((x - 0.5) / 0.18) ** 2)))[None, :] * y
    truth = np.stack([level * 1.0, level * 0.55, level * 0.25], -1).astype(np.float32)
    return truth, truth.copy()


def _clip(truth: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    clipped = truth >= CLIP
    return np.minimum(truth, CLIP).astype(np.float32), clipped


def test_clipped_red_is_rebuilt_from_green_and_blue() -> None:
    truth, _ = _orange()
    rgb, mask = _clip(truth)
    assert mask[..., 0].any()
    assert not mask[..., 1:].any()
    out = recover_highlights(rgb, mask, 1.0)
    area = mask[..., 0]
    before = np.abs(rgb[area, 0] - truth[area, 0]).mean()
    after = np.abs(out[area, 0] - truth[area, 0]).mean()
    assert after <= 0.3 * before
    ratio = out[area, 0] / out[area, 1]
    np.testing.assert_allclose(ratio, truth[area, 0] / truth[area, 1], rtol=0.10)


def test_unclipped_pixels_are_untouched() -> None:
    truth, _ = _orange()
    rgb, mask = _clip(truth)
    out = recover_highlights(rgb, mask, 1.0)
    untouched = ~mask.any(-1)
    np.testing.assert_array_equal(out[untouched], rgb[untouched])


def test_strength_zero_returns_the_input() -> None:
    truth, _ = _orange()
    rgb, mask = _clip(truth)
    np.testing.assert_array_equal(recover_highlights(rgb, mask, 0.0), rgb)


def test_pixels_clipped_in_every_channel_are_unchanged() -> None:
    rgb = np.full((16, 16, 3), CLIP, np.float32)
    rgb[:, :4] = (0.5, 0.3, 0.2)
    mask = np.zeros((16, 16, 3), bool)
    mask[:, 4:] = True
    out = recover_highlights(rgb, mask, 1.0)
    np.testing.assert_array_equal(out[:, 4:], rgb[:, 4:])


def test_a_frame_without_clipping_comes_back_unchanged() -> None:
    truth, _ = _orange()
    rgb = (truth * 0.5).astype(np.float32)
    out = recover_highlights(rgb, np.zeros(rgb.shape, bool), 1.0)
    np.testing.assert_array_equal(out, rgb)


@pytest.mark.parametrize("strength", [0.25, 0.5, 0.75])
def test_strength_blends_between_off_and_full(strength: float) -> None:
    truth, _ = _orange()
    rgb, mask = _clip(truth)
    full = recover_highlights(rgb, mask, 1.0)
    part = recover_highlights(rgb, mask, strength)
    np.testing.assert_allclose(part, rgb + strength * (full - rgb), atol=1e-6)
