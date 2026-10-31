"""The frame timing report (measured rates, dropped and duplicated frames) and the frame-rate targets."""

import json
from collections.abc import Callable
from fractions import Fraction
from pathlib import Path

import numpy as np
import pytest
from click.testing import CliRunner

from mcrawsome.cli.main import cli
from mcrawsome.io.describe import duplicated_frames, frame_timing, target_rate

STEP = 33_333_333  # 30 fps


def _times(slots: list[float], step: float = STEP) -> np.ndarray:
    return np.array([round(s * step) for s in slots], np.int64)


def test_dropped_and_duplicated_frames() -> None:
    times = _times([0, 1, 2, 4, 5, 5.05, 6, 7, 8])  # frame 3 dropped; 5 delivered twice
    assert duplicated_frames(times) == 1
    timing = frame_timing(times)
    assert timing.frames == 9
    assert timing.dropped == 1
    assert timing.duplicated == 1
    assert timing.median_fps == pytest.approx(30.0, rel=1e-6)
    assert timing.average_fps == pytest.approx(8 / (8 * STEP / 1e9), rel=1e-6)


def test_targets() -> None:
    times = _times(list(range(100)), 1e9 / 30.33)
    assert target_rate("keep", times) is None
    assert target_rate("integer", times) == Fraction(30)
    assert target_rate("drop-frame", times) == Fraction(30000, 1001)
    assert float(target_rate("median", times) or 0) == pytest.approx(30.33, abs=0.01)
    assert float(target_rate("average", times) or 0) == pytest.approx(30.33, abs=0.01)
    assert target_rate(25.0, times) == Fraction(25)
    pal = _times(list(range(100)), 1e9 / 25)
    assert target_rate("drop-frame", pal) == Fraction(25)  # no drop-frame rate near 25
    assert target_rate("drop-frame", _times(list(range(100)), 1e9 / 59.9)) == Fraction(60000, 1001)


def test_the_report_lists_the_targets() -> None:
    timing = frame_timing(_times(list(range(100)), 1e9 / 30.33))
    assert set(timing.targets) == {"integer", "drop-frame", "average", "median"}
    assert timing.targets["integer"] == 30.0


def test_info_reports_the_timing(tmp_path: Path, make_mcraw: Callable[..., Path]) -> None:
    clip = make_mcraw(tmp_path / "clip.mcraw", [i * 41_666_667 for i in (0, 1, 2, 4, 5)])
    result = CliRunner().invoke(cli, ["info", str(clip), "--json"])
    assert result.exit_code == 0, result.output
    timing = json.loads(result.output)["timing"]
    assert timing["frames"] == 5
    assert timing["dropped"] == 1
    assert timing["duplicated"] == 0
    assert timing["median_fps"] == pytest.approx(24.0, rel=1e-3)
    assert timing["targets"]["integer"] == 24.0
    text = CliRunner().invoke(cli, ["info", str(clip)], terminal_width=200).output
    assert "Frame rate" in text
    assert "Duplicated frames" in text
    assert "Frame-rate targets" in text
