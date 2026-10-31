import json
from pathlib import Path

import numpy as np
import pytest

from mcrawsome.develop.raw import Developer, median_neutral
from mcrawsome.io.clip import Clip
from mcrawsome.io.metadata import FrameMetadata, parse_container_metadata
from mcrawsome.preview.backend import PreviewBackend

DATA = Path(__file__).parent.parent / "data"
CONTAINER = parse_container_metadata(json.loads((DATA / "container_metadata_pixel10.json").read_text()))
NEUTRAL = (0.5, 1.0, 0.6)
SITES = {"r": 0, "g": 1, "b": 2}


def _frame(orientation: int = 2) -> FrameMetadata:
    return FrameMetadata.model_validate(
        {
            "width": 32,
            "height": 16,
            "originalWidth": 32,
            "originalHeight": 16,
            "orientation": orientation,
            "compressionType": 7,
            "asShotNeutral": list(NEUTRAL),
            "dynamicBlackLevel": [256] * 4,
            "dynamicWhiteLevel": 4095,
            "iso": 50,
            "exposureTime": 1e7,
            "lensShadingMap": [[1.0] * 4] * 4,
            "lensShadingMapWidth": 2,
            "lensShadingMapHeight": 2,
            "recvdTimestampMs": 0,
        }
    )


def _grey_mosaic(level: float) -> np.ndarray:
    m = np.zeros((16, 32), np.uint16)
    for i, colour in enumerate(CONTAINER.sensor_arrangement):
        y, x = divmod(i, 2)
        m[y::2, x::2] = round(256 + level * (4095 - 256) * NEUTRAL[SITES[colour]])
    return m


@pytest.mark.usefixtures("full_develop")
def test_grey_subject_develops_to_neutral_grey() -> None:
    out = Developer(CONTAINER, _frame(), NEUTRAL).develop(_grey_mosaic(0.2))
    centre = out[4:-4, 4:-4].reshape(-1, 3).mean(axis=0)
    np.testing.assert_allclose(centre, [0.2, 0.2, 0.2], atol=3e-3)


@pytest.mark.usefixtures("full_develop")
def test_clipped_highlights_are_neutral() -> None:
    raw = np.full((16, 32), 4095, np.uint16)
    out = Developer(CONTAINER, _frame(), NEUTRAL).develop(raw)
    px = out[8, 16]
    assert px.min() >= 0.99
    assert float(px.max() - px.min()) < 0.02


@pytest.mark.usefixtures("full_develop")
def test_upside_down_frames_are_rotated() -> None:
    raw = _grey_mosaic(0.2)
    raw[:2, :2] = 4000  # bright top-left corner in sensor orientation
    out = Developer(CONTAINER, _frame(orientation=3), NEUTRAL).develop(raw)
    assert out[-1, -1].mean() > out[0, 0].mean()


def test_median_neutral() -> None:
    assert median_neutral([(0.5, 1, 0.6), (0.6, 1, 0.5), (0.55, 1, 0.7)]) == (0.55, 1.0, 0.6)


@pytest.mark.fixture
@pytest.mark.usefixtures("full_develop")
def test_matches_prototype_reference(fixture_12bit: Path) -> None:
    ref_path = Path(__file__).parent.parent / "fixtures" / "data" / "ref_develop_12bit.npz"
    if not ref_path.exists():
        pytest.skip("run scripts/make_references.py first")
    ref = np.load(ref_path)["image"]
    with Clip(fixture_12bit) as clip:
        frame = clip.raw_frame(0)
        out = Developer(
            clip.metadata, frame.metadata, frame.metadata.as_shot_neutral, highlight_recovery=0.0
        ).develop(frame.data)
    import cv2

    small = cv2.resize(out, (ref.shape[1], ref.shape[0]), interpolation=cv2.INTER_AREA)
    assert float(np.abs(small - ref).mean()) < 1e-3


def _orange_with_red_clipped() -> np.ndarray:
    """Red photosites at sensor white across the frame, green and blue well below it."""
    m = np.zeros((16, 32), np.uint16)
    for i, colour in enumerate(CONTAINER.sensor_arrangement):
        y, x = divmod(i, 2)
        m[y::2, x::2] = {"r": 4095, "g": 256 + 1500, "b": 256 + 500}[colour]
    return m


@pytest.mark.usefixtures("full_develop")
def test_highlight_recovery_keeps_the_colour_of_a_red_clipped_area() -> None:
    raw = _orange_with_red_clipped()
    out = Developer(CONTAINER, _frame(), NEUTRAL, highlight_recovery=1.0).develop(raw)
    off = Developer(CONTAINER, _frame(), NEUTRAL, highlight_recovery=0.0).develop(raw)

    def spread(px: np.ndarray) -> float:
        return float(px.max() - px.min())

    assert spread(out[8, 16]) > 5 * spread(off[8, 16])  # recovered: still coloured, not neutral grey


def _vignetted_flat_field(clip: Clip) -> np.ndarray:
    """What the sensor records of an evenly lit white wall: half of white, darkened by the lens."""
    from mcrawsome.camera.shading import shading_gains

    frame = clip.frame_metadata(0)
    gains = shading_gains(frame)
    black, white = float(frame.black_level[0]), float(frame.white_level)
    r_row = SITE_ROWS[clip.metadata.sensor_arrangement.index("r")]
    raw = np.empty((frame.height, frame.width), np.uint16)
    for i, colour in enumerate(clip.metadata.sensor_arrangement):
        y, x = divmod(i, 2)
        plane = gains[0] if colour == "r" else gains[3] if colour == "b" else gains[1 if y == r_row else 2]
        raw[y::2, x::2] = np.round(black + 0.5 * (white - black) / plane).astype(np.uint16)
    return raw


SITE_ROWS = (0, 0, 1, 1)


def _centre_to_corner(image: np.ndarray) -> float:
    h, w = image.shape[:2]
    luma = image.mean(-1)
    centre = float(luma[h // 2 - 20 : h // 2 + 20, w // 2 - 20 : w // 2 + 20].mean())
    corner = float(luma[8:48, 8:48].mean())
    return centre / corner


@pytest.mark.fixture
@pytest.mark.usefixtures("full_develop")
def test_lens_shading_strength_keeps_or_removes_the_vignette(fixture_12bit: Path) -> None:
    from mcrawsome.camera.shading import shading_gains

    with Clip(fixture_12bit) as clip:
        raw = _vignetted_flat_field(clip)
        frame, neutral = clip.frame_metadata(0), clip.frame_metadata(0).as_shot_neutral
        g = shading_gains(frame)[1]
        hy, hx = g.shape[0] // 2, g.shape[1] // 2  # the planes are at half resolution
        lens = float(g[4:24, 4:24].mean() / g[hy - 10 : hy + 10, hx - 10 : hx + 10].mean())

        def ratio(strength: float) -> float:
            dev = Developer(clip.metadata, frame, neutral, lens_shading=strength)
            return _centre_to_corner(dev.develop(raw))

        assert ratio(1.0) == pytest.approx(1.0, rel=0.02)
        assert ratio(0.0) == pytest.approx(lens, rel=0.03)
        assert 1.0 < ratio(0.5) < lens


def _white_light_with_green_clipped() -> np.ndarray:
    """A white lamp: green photosites at sensor white, red and blue (white-balanced) a little brighter."""
    m = np.zeros((16, 32), np.uint16)
    for i, colour in enumerate(CONTAINER.sensor_arrangement):
        y, x = divmod(i, 2)
        m[y::2, x::2] = {"r": 2800, "g": 4095, "b": 3300}[colour]
    return m


@pytest.mark.usefixtures("full_develop")
def test_a_partial_highlight_recovery_strength_lies_between_off_and_full() -> None:
    raw = _white_light_with_green_clipped()

    def green_over_red(strength: float) -> float:
        px = Developer(CONTAINER, _frame(), NEUTRAL, highlight_recovery=strength).develop(raw)[8, 16]
        return float(px[1] / px[0])

    off, half, full = green_over_red(0.0), green_over_red(0.5), green_over_red(1.0)
    assert min(off, full) - 1e-3 <= half <= max(off, full) + 1e-3, (off, half, full)


@pytest.mark.parametrize("cfa", ["rggb", "bggr", "grbg", "gbrg"])
@pytest.mark.parametrize("orientation", [0, 3])
def test_native_preview_preserves_bayer_gains_colour_and_orientation(
    cfa: str, orientation: int, preview_compute: PreviewBackend
) -> None:
    container = CONTAINER.model_copy(update={"sensor_arrangement": cfa})
    developer = Developer(container, _frame(orientation), NEUTRAL)
    raw = np.random.default_rng(4).integers(300, 1800, (16, 32), dtype=np.uint16)
    expected = developer.develop_half(raw)
    np.testing.assert_allclose(preview_compute.develop(developer, raw, 2, 65536), expected, atol=2e-7)
    quarter = expected.reshape(4, 2, 8, 2, 3).mean(axis=(1, 3))
    np.testing.assert_allclose(preview_compute.develop(developer, raw, 4, 65536), quarter, atol=2e-7)
    assert preview_compute.develop(developer, raw, 2, 12).shape == (6, 12, 3)


def test_native_preview_rejects_mismatched_planes_and_invalid_sizes() -> None:
    from mcrawsome import _raw

    developer = Developer(CONTAINER, _frame(), NEUTRAL)
    raw = _grey_mosaic(0.2)
    with pytest.raises(ValueError, match="invalid preview"):
        developer.develop_fast(raw, step=3)
    with pytest.raises(ValueError, match="width must be positive"):
        developer.develop_fast(raw, width=0)
    with pytest.raises(ValueError, match="invalid preview"):
        _raw.develop_preview(raw[:4], developer.black, developer.gain, developer.cfa)


def test_native_exposure_and_log_match_the_full_transfer() -> None:
    from mcrawsome import _raw
    from mcrawsome.colour.transfer import acescct_encode

    linear = np.linspace(-0.1, 3, 240, dtype=np.float32).reshape(8, 10, 3)
    for stops in (-2.0, 0.0, 1.5):
        np.testing.assert_allclose(
            _raw.encode_preview(linear, stops), acescct_encode(linear * 2**stops), atol=2e-7
        )


def test_fast_clipped_white_stays_neutral(preview_compute: PreviewBackend) -> None:
    raw = _white_light_with_green_clipped()
    developer = Developer(CONTAINER, _frame(), NEUTRAL)
    fast = preview_compute.develop(developer, raw, 2, 65536)
    full = developer.develop_half(raw)
    np.testing.assert_allclose(fast.mean(axis=(0, 1)), full.mean(axis=(0, 1)), atol=0.02)


def test_fast_red_clipped_area_keeps_the_full_develops_colour(preview_compute: PreviewBackend) -> None:
    raw = _orange_with_red_clipped()
    developer = Developer(CONTAINER, _frame(), NEUTRAL)
    np.testing.assert_allclose(
        preview_compute.develop(developer, raw, 2, 65536), developer.develop_half(raw), atol=0.02
    )


@pytest.mark.parametrize("strength", [0.0, 0.5, 1.0])
def test_native_preview_matches_spatial_highlight_recovery(
    strength: float, preview_compute: PreviewBackend
) -> None:
    developer = Developer(CONTAINER, _frame(), NEUTRAL, highlight_recovery=strength)
    raw = np.random.default_rng(12).integers(300, 3900, (16, 32), dtype=np.uint16)
    raw[2:6, 2:6] = 4095
    raw[8:14:2, 16:28:2] = 4095
    np.testing.assert_allclose(
        preview_compute.develop(developer, raw, 2, 65536), developer.develop_half(raw), atol=2e-6
    )


def test_preview_log_covers_the_linear_knee_and_highlight_range() -> None:
    from mcrawsome import _raw
    from mcrawsome.colour.transfer import acescct_encode

    linear = np.geomspace(1e-8, 65536, 60000, dtype=np.float32).reshape(100, 200, 3)
    np.testing.assert_allclose(_raw.encode_preview(linear, 0), acescct_encode(linear), atol=2e-7, rtol=0)


def test_fast_shading_and_per_site_black_levels_match(preview_compute: PreviewBackend) -> None:
    developer = Developer(CONTAINER, _frame(), NEUTRAL, black_level=(200, 230, 250, 270))
    yy, xx = np.mgrid[:16, :32].astype(np.float32)
    developer.gain *= 0.8 + 0.4 * xx / 32 + 0.1 * yy / 16
    raw = np.random.default_rng(42).integers(300, 1800, (16, 32), dtype=np.uint16)
    np.testing.assert_allclose(
        preview_compute.develop(developer, raw, 2, 65536), developer.develop_half(raw), atol=3e-7
    )


def test_explicit_gains_replace_the_lens_shading_map() -> None:
    frame = _frame()
    ones = tuple(np.ones((8, 16), np.float32) for _ in range(4))
    twos = tuple(np.full((8, 16), 2.0, np.float32) for _ in range(4))
    base = Developer(CONTAINER, frame, NEUTRAL, gains=ones).develop(_grey_mosaic(0.1))  # type: ignore[arg-type]
    doubled = Developer(CONTAINER, frame, NEUTRAL, gains=twos).develop(_grey_mosaic(0.1))  # type: ignore[arg-type]
    np.testing.assert_allclose(doubled, 2 * base, rtol=1e-5)


def test_explicit_gains_follow_the_lens_shading_strength() -> None:
    frame = _frame()
    twos = tuple(np.full((8, 16), 4.0, np.float32) for _ in range(4))
    half = Developer(CONTAINER, frame, NEUTRAL, lens_shading=0.5, gains=twos).develop(_grey_mosaic(0.1))  # type: ignore[arg-type]
    plain = Developer(CONTAINER, frame, NEUTRAL).develop(_grey_mosaic(0.1))
    np.testing.assert_allclose(half, 2 * plain, rtol=1e-5)


def test_the_default_matches_explicit_shading_gains_and_demosaic() -> None:
    """The defaults are the video path: the frame's shading map and Malvar-He-Cutler, bit for bit."""
    from mcrawsome.camera.shading import shading_gains
    from mcrawsome.develop.demosaic import demosaic

    rng = np.random.default_rng(5)
    raw = rng.integers(256, 4095, (16, 32), dtype=np.uint16)
    raw[2:5, 3:9] = 4095
    frame = _frame()
    default = Developer(CONTAINER, frame, NEUTRAL, 0.5, 0.7)
    explicit = Developer(CONTAINER, frame, NEUTRAL, 0.5, 0.7, gains=shading_gains(frame), demosaic=demosaic)
    np.testing.assert_array_equal(default.develop(raw), explicit.develop(raw))
    np.testing.assert_array_equal(default.develop_half(raw), explicit.develop_half(raw))


def test_a_custom_demosaic_is_used() -> None:
    calls: list[str] = []

    def flat(mosaic: np.ndarray, cfa: str) -> np.ndarray:
        calls.append(cfa)
        return np.repeat(mosaic[..., None], 3, axis=2).astype(np.float32)

    Developer(CONTAINER, _frame(), NEUTRAL, demosaic=flat).develop(_grey_mosaic(0.1))
    assert calls == [CONTAINER.sensor_arrangement]


def test_develop_is_camera_rgb_through_the_matrix() -> None:
    rng = np.random.default_rng(6)
    raw = rng.integers(256, 4095, (16, 32), dtype=np.uint16)
    developer = Developer(CONTAINER, _frame(), NEUTRAL)
    camera = developer.camera_rgb(raw)
    assert camera.shape == (16, 32, 3)
    np.testing.assert_array_equal(developer.develop(raw), developer.to_ap1(camera))
