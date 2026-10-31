"""The isolated GPU spike's data contract and numerical checks, without local clips."""

import importlib
from pathlib import Path
from types import ModuleType

import numpy as np
import pytest


@pytest.fixture
def trial(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    return importlib.import_module("gpu_trial.common")


def test_only_the_explicit_approved_clip_is_accepted(trial: ModuleType, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="approved"):
        trial.approved_clip(tmp_path / "other.mcraw")
    path = tmp_path / "261004_174122_VIDEO_26mm.mcraw"
    path.touch()
    assert trial.approved_clip(path) == path
    link = tmp_path / "alias.mcraw"
    link.symlink_to(path)
    with pytest.raises(ValueError, match="approved"):
        trial.approved_clip(link)


def test_reduced_bayer_averages_each_colour_site_separately(trial: ModuleType) -> None:
    raw = np.arange(8 * 12, dtype=np.uint16).reshape(8, 12)
    small = trial.reduce_bayer(raw)
    assert small.shape == (4, 6)
    assert small.dtype == np.uint16
    for y in range(2):
        for x in range(2):
            plane = raw[y::2, x::2].reshape(2, 2, 3, 2)
            expected = np.floor(plane.mean(axis=(1, 3)) + 0.5).astype(np.uint16)
            np.testing.assert_array_equal(small[y::2, x::2], expected)
    with pytest.raises(ValueError, match="multiple of four"):
        trial.reduce_bayer(raw[:6])


def test_scrub_order_reverses_and_stays_in_bounds(trial: ModuleType) -> None:
    assert trial.frame_order(4, "scrub", 10) == [0, 1, 2, 3, 2, 1, 0, 1, 2, 3]
    assert trial.frame_order(4, "playback", 6) == [0, 1, 2, 3, 0, 1]


def test_raw_reduction_matches_the_exporters_tie_rounding(trial: ModuleType) -> None:
    from mcrawsome.output.dng import half_cfa

    raw = np.zeros((4, 4), np.uint16)
    raw[2, 2] = 2  # mean 0.5 at the red site rounds to even, as the DNG helper does
    np.testing.assert_array_equal(trial.reduce_bayer(raw), half_cfa(raw, "rggb"))
