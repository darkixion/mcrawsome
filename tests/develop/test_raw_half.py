"""Developer.develop_half: each 2x2 Bayer quad becomes one pixel (the preview's develop)."""

import json
from pathlib import Path

import cv2
import numpy as np
import pytest

from mcrawsome.develop.raw import Developer
from mcrawsome.io.clip import Clip
from mcrawsome.io.metadata import FrameMetadata, parse_container_metadata

DATA = Path(__file__).parent.parent / "data"
CONTAINER = parse_container_metadata(json.loads((DATA / "container_metadata_pixel10.json").read_text()))
NEUTRAL = (0.5, 1.0, 0.6)
CHANNEL = {"r": 0, "g": 1, "b": 2}
BLACK, WHITE = 256, 4095
H, W = 16, 32


def _frame(orientation: int = 2) -> FrameMetadata:
    return FrameMetadata.model_validate(
        {
            "width": W,
            "height": H,
            "originalWidth": W,
            "originalHeight": H,
            "orientation": orientation,
            "compressionType": 7,
            "asShotNeutral": list(NEUTRAL),
            "dynamicBlackLevel": [BLACK] * 4,
            "dynamicWhiteLevel": WHITE,
            "iso": 50,
            "exposureTime": 1e7,
            "lensShadingMap": [[1.0] * 4] * 4,
            "lensShadingMapWidth": 2,
            "lensShadingMapHeight": 2,
            "recvdTimestampMs": 0,
        }
    )


def _random_quads(seed: int = 1) -> np.ndarray:
    """Each photosite a known value well below sensor white (no clipping)."""
    rng = np.random.default_rng(seed)
    return rng.integers(BLACK + 50, 2400, (H, W)).astype(np.uint16)


def _expected(dev: Developer, raw: np.ndarray) -> np.ndarray:
    """(R, mean of the two G, B) of each quad, normalised and white-balanced, through the matrix."""
    scale = 1.0 / (WHITE - BLACK)
    rgb = np.zeros((H // 2, W // 2, 3), np.float64)
    count = np.zeros(3)
    for i, colour in enumerate(CONTAINER.sensor_arrangement):
        y, x = divmod(i, 2)
        c = CHANNEL[colour]
        rgb[..., c] += (raw[y::2, x::2].astype(np.float64) - BLACK) * scale / NEUTRAL[c]
        count[c] += 1
    rgb /= count
    return np.asarray(rgb @ np.asarray(dev.matrix, np.float64).T, np.float32)


def test_each_quad_becomes_one_pixel_through_the_matrix() -> None:
    dev = Developer(CONTAINER, _frame(), NEUTRAL)
    raw = _random_quads()
    out = dev.develop_half(raw)
    assert out.shape == (H // 2, W // 2, 3)
    assert out.dtype == np.float32
    np.testing.assert_allclose(out, _expected(dev, raw), rtol=1e-5, atol=1e-6)


def test_upside_down_frames_are_turned() -> None:
    raw = _random_quads(2)
    upright = Developer(CONTAINER, _frame(), NEUTRAL).develop_half(raw)
    turned = Developer(CONTAINER, _frame(orientation=3), NEUTRAL).develop_half(raw)
    np.testing.assert_array_equal(turned, upright[::-1, ::-1])


def _flat(levels: dict[str, int]) -> np.ndarray:
    m = np.zeros((H, W), np.uint16)
    for i, colour in enumerate(CONTAINER.sensor_arrangement):
        y, x = divmod(i, 2)
        m[y::2, x::2] = levels[colour]
    return m


@pytest.mark.parametrize("strength", [0.0, 0.5, 1.0])
@pytest.mark.parametrize(
    "levels",
    [
        {"r": 2800, "g": 4095, "b": 3300},  # a white lamp: green clipped
        {"r": 4095, "g": 1756, "b": 756},  # orange: red clipped
        {"r": 4095, "g": 4095, "b": 4095},  # everything clipped
    ],
)
def test_a_flat_clipped_area_develops_as_develop_does(levels: dict[str, int], strength: float) -> None:
    raw = _flat(levels)
    dev = Developer(CONTAINER, _frame(), NEUTRAL, highlight_recovery=strength)
    full = dev.develop(raw)[H // 2, W // 2]
    half = dev.develop_half(raw)
    np.testing.assert_allclose(half[H // 4, W // 4], full, rtol=2e-3, atol=2e-4)


def test_one_clipped_green_site_marks_its_quad_green_as_clipped() -> None:
    """A quad with one clipped green photosite counts as clipped in green, as a flat clipped area would."""
    lamp = {"r": 2800, "g": 4095, "b": 3300}
    raw = _flat(lamp)
    g_sites = [divmod(i, 2) for i, c in enumerate(CONTAINER.sensor_arrangement) if c == "g"]
    (y, x) = g_sites[0]
    raw[y::2, x::2] = 3000  # the other green site of every quad stays clipped
    dev = Developer(CONTAINER, _frame(), NEUTRAL, highlight_recovery=0.0)
    flat = dev.develop(_flat(lamp))[H // 2, W // 2]
    np.testing.assert_allclose(dev.develop_half(raw)[H // 4, W // 4], flat, rtol=2e-3, atol=2e-4)


@pytest.mark.fixture
def test_matches_develop_on_a_real_frame(fixture_12bit: Path) -> None:
    with Clip(fixture_12bit) as clip:
        frame = clip.raw_frame(0)
        dev = Developer(clip.metadata, frame.metadata, frame.metadata.as_shot_neutral)
        full = dev.develop(frame.data)
        half = dev.develop_half(frame.data)
    assert half.shape == (full.shape[0] // 2, full.shape[1] // 2, 3)
    small = cv2.resize(full, (half.shape[1], half.shape[0]), interpolation=cv2.INTER_AREA)
    assert float(np.abs(small - half).mean()) / 0.18 < 0.03
