"""The preview's fast cheap end matches the export's finish_frame and the display table."""

from pathlib import Path

import numpy as np
import pytest

from mcrawsome import _raw
from mcrawsome.colour.cube import apply_cube
from mcrawsome.colour.display import display_table
from mcrawsome.pipeline.render import FrameJob, finish_frame
from mcrawsome.preview.backend import PreviewBackend
from mcrawsome.preview.finish import Finisher, codes16, display_bgr


def _scene(width: int = 480, height: int = 270) -> np.ndarray:
    """Scene-linear light with smooth gradients, a few highlights and noise."""
    rng = np.random.default_rng(5)
    yy, xx = np.mgrid[0:height, 0:width].astype(np.float32)
    base = 0.02 + 0.6 * (xx / width)[..., None] * np.array([1.0, 0.8, 0.6], np.float32)
    base = base + 0.3 * (yy / height)[..., None] * np.array([0.2, 0.5, 1.0], np.float32)
    base[100:120, 200:230] = 6.0
    noise = rng.normal(0.0, 0.01, base.shape).astype(np.float32)
    return np.asarray(np.maximum(base + noise, 0.0), dtype=np.float32)


def _reference(linear: np.ndarray, stops: float, rotation: int) -> np.ndarray:
    job = FrameJob(
        clip_path=Path("x.mcraw"), exposure_stops=stops, neutral=(1.0, 1.0, 1.0), fps=24.0, rotation=rotation
    )
    return finish_frame(job, linear, 7)


def _display(codes: np.ndarray) -> np.ndarray:
    """16-bit ACEScct codes through the display table, as 8-bit RGB."""
    return np.round(apply_cube(display_table(), codes.astype(np.float32) / 65535) * 255)


@pytest.mark.parametrize("stops", [-1.0, 0.0, 1.7])
def test_the_fast_finish_matches_finish_frame(stops: float) -> None:
    linear = _scene()
    fast = codes16(Finisher(linear, 90).encoded(stops))
    reference = _reference(linear, stops, 90)
    assert fast.shape == reference.shape == (480, 270, 3)  # turned for display
    difference = np.abs(fast.astype(np.int64) - reference)
    assert difference.max() <= 66  # 1/1000 of full scale: float32 rounding in a different order
    assert difference.mean() < 2


def test_the_last_exposure_is_kept() -> None:
    finisher = Finisher(_scene(), 0)
    first = finisher.encoded(0.5)
    assert finisher.encoded(0.5) is first
    assert finisher.encoded(0.6) is not first


def test_the_native_display_matches_the_table_to_one_code_value() -> None:
    rng = np.random.default_rng(2)
    frame = rng.random((64, 96, 3), dtype=np.float32)
    frame[:4] = rng.normal(0.5, 1.0, (4, 96, 3))  # outside 0 to 1: clipped first
    bgr = display_bgr(frame, display_table())
    expected = _display(codes16(frame))[..., ::-1]
    assert bgr.dtype == np.uint8
    assert np.abs(bgr.astype(np.int64) - expected).max() <= 1


def test_the_native_display_refuses_a_table_that_is_not_a_cube() -> None:
    with pytest.raises(ValueError, match="size"):
        _raw.apply_look(np.zeros((2, 2, 3), np.float32), np.zeros((10, 3), np.float32))
    with pytest.raises(ValueError, match="height, width, 3"):
        _raw.apply_look(np.zeros((2, 2), np.float32), np.zeros((8, 3), np.float32))


def test_a_draft_matches_the_export_finish_through_the_display() -> None:
    linear = _scene()
    fast = display_bgr(Finisher(linear, 0).encoded(0.3), display_table())
    expected = _display(_reference(linear, 0.3, 0))[..., ::-1]
    difference = np.abs(fast.astype(np.int64) - expected)
    assert difference.max() <= 2
    assert difference.mean() < 0.05


@pytest.mark.parametrize("stops", [-2.0, 0.0, 1.5])
def test_fused_playback_matches_separate_exposure_and_display(
    stops: float, preview_compute: PreviewBackend
) -> None:
    linear = np.linspace(-0.05, 4, 32 * 48 * 3, dtype=np.float32).reshape(32, 48, 3)
    table = display_table()
    expected = display_bgr(_raw.encode_preview(linear, stops), table)
    actual = preview_compute.finish(linear, table, stops)
    assert np.abs(actual.astype(np.int16) - expected).max() <= 1
