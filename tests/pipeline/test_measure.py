from pathlib import Path

import numpy as np
import pytest

from mcrawsome.colour.cube import bake
from mcrawsome.io.clip import Clip
from mcrawsome.pipeline.measure import (
    SdrLight,
    frame_code,
    measure_sdr,
    sample_indices,
    sdr_brightness_warning,
)
from mcrawsome.pipeline.render import FrameJob, choose_neutral


def test_frame_code_of_a_flat_table() -> None:
    table = bake(lambda x: np.broadcast_to(np.array([0.5, 0.4, 0.2]), x.shape).copy(), size=3)
    median, p90 = frame_code(np.full((40, 64, 3), 30000, np.uint16), table)
    y = 0.2126 * 0.5 + 0.7152 * 0.4 + 0.0722 * 0.2
    assert median == pytest.approx(y, rel=1e-3)
    assert p90 == pytest.approx(y, rel=1e-3)


def test_samples_are_spread_over_the_clip() -> None:
    assert sample_indices(5) == [0, 1, 2, 3, 4]
    assert sample_indices(100, 3) == [0, 50, 99]


def test_brightness_warnings() -> None:
    assert sdr_brightness_warning(SdrLight(0.4, 0.6)) is None
    message = sdr_brightness_warning(SdrLight(0.1, 0.3))
    assert message is not None
    assert "dark" in message
    assert "--develop.exposure +1" in message
    message = sdr_brightness_warning(SdrLight(0.8, 0.9))
    assert message is not None
    assert "bright" in message


@pytest.mark.fixture
@pytest.mark.slow
def test_measure_a_real_clip(fixture_12bit: Path) -> None:
    with Clip(fixture_12bit) as clip:
        job = FrameJob(clip_path=fixture_12bit, exposure_stops=0.0, neutral=choose_neutral(clip, "median"))
    light = measure_sdr(job, 3, workers=2)
    assert 0 < light.median_code <= light.p90_code < 1
