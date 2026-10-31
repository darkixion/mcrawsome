import shutil
from pathlib import Path

import numpy as np
import pytest

from mcrawsome.io.clip import Clip
from mcrawsome.io.describe import (
    describe,
    describe_video,
    dropped_frames,
    focus_text,
    shutter_text,
    span_text,
)


def test_video_info_names_the_space_and_transfer_function(synthetic_video: Path) -> None:
    path = Path(shutil.move(synthetic_video, synthetic_video.with_name("clip_Davinci_Davinci-WG.mp4")))
    video = dict(dict(describe_video(path))["Video"])
    assert video["Colour space"] == "DaVinci Wide Gamut"
    assert video["Transfer function"] == "DaVinci Intermediate"
    assert video["Colour source"] == "Recording profile metadata"
    assert "Colour encoding" not in video


def test_shutter_as_fraction_and_angle() -> None:
    assert shutter_text([20.83e6], 24.0) == "1/48 s (20.83 ms, 180° shutter)"
    assert shutter_text([10.0e6, 20.0e6], 24.0) == "1/100 to 1/50 s (10.00 to 20.00 ms, 86° to 173° shutter)"


def test_focus_distance_from_dioptres() -> None:
    assert focus_text([0.0]) == "infinity"
    assert focus_text([2.6245]) == "0.38 m (2.62 dioptres)"
    assert focus_text([2.0, 4.0]) == "0.25 to 0.50 m (changed during the clip)"


def test_span_of_values() -> None:
    assert span_text([21, 21, 21], "{:.0f}") == "21"
    assert span_text([21, 50, 160], "{:.0f}") == "21 to 160 (median 50)"


def test_dropped_frames_from_timestamps() -> None:
    step = int(1e9 / 24)
    steady = np.arange(10, dtype=np.int64) * step
    assert dropped_frames(steady) == 0
    gap = np.delete(steady, [4, 7])
    assert dropped_frames(gap) == 2


@pytest.mark.fixture
def test_describe_a_real_clip(fixture_12bit: Path) -> None:
    with Clip(fixture_12bit) as clip:
        sections = dict(describe(clip))
    assert set(sections) >= {"Camera", "Lens", "Sensor", "Exposure", "Recording", "MotionCam settings"}
    lens = dict(sections["Lens"])
    assert "mm" in lens["Focal length"]
    assert lens["Aperture"].startswith("f/")
    exposure = dict(sections["Exposure"])
    assert "shutter" in exposure["Shutter"]
    assert exposure["White balance"].endswith("K")
    recording = dict(sections["Recording"])
    assert "Started" in recording
    assert recording["Dropped frames"] == "none"


@pytest.mark.fixture
def test_focal_length_is_labelled_as_an_equivalent(fixture_12bit: Path) -> None:
    with Clip(fixture_12bit) as clip:
        lens = dict(dict(describe(clip))["Lens"])
    assert lens["Focal length"].endswith("mm (35 mm equivalent)")
