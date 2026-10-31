"""Frame-rate conversion: output frames mapped to recorded timestamps at a constant target rate."""

import itertools
import tomllib
import wave
from collections.abc import Callable
from fractions import Fraction
from pathlib import Path

import numpy as np
import pytest

from mcrawsome.io.clip import Clip
from mcrawsome.io.describe import rate_map
from mcrawsome.output.dng import read_dng_tags
from mcrawsome.output.dng_export import export_dng
from mcrawsome.output.export import clip_sources, frame_range_for, ignored_settings, read_clip_facts
from mcrawsome.recipe.load import build_recipe

STEP = 41_666_667  # 24 fps


def test_a_regular_clip_at_its_own_rate_is_every_frame() -> None:
    times = np.arange(50, dtype=np.int64) * STEP
    assert rate_map(times, STEP, 20e6) == list(range(50))


def test_a_faster_clip_drops_frames_within_the_drift_limit() -> None:
    step = 1e9 / 30.33
    times = np.round(np.arange(400) * step).astype(np.int64)
    period, limit = 1e9 / 30, 20e6
    sources = rate_map(times, period, limit)
    assert len(sources) == round(400 / 30.33 * 30)  # the recording's length at the new rate
    skipped = sum(b - a - 1 for a, b in itertools.pairwise(sources))
    assert skipped == pytest.approx(400 * (1 - 30 / 30.33), abs=2)  # about one frame in 90
    assert all(b >= a for a, b in itertools.pairwise(sources))
    drift = [abs(times[s] - k * period) for k, s in enumerate(sources)]
    assert max(drift) <= limit + 1


def test_a_slower_clip_repeats_frames() -> None:
    times = np.round(np.arange(240) * 1e9 / 23.5).astype(np.int64)
    sources = rate_map(times, STEP, 20e6)
    repeats = sum(1 for a, b in itertools.pairwise(sources) if a == b)
    assert repeats == pytest.approx(len(sources) - 240, abs=1)


def test_a_dropped_frame_becomes_a_repeat() -> None:
    times = np.array([0, 1, 2, 4, 5], dtype=np.int64) * STEP
    assert rate_map(times, STEP, 20e6) == [0, 1, 2, 2, 3, 4]


def test_the_limit_is_at_least_half_a_frame() -> None:
    jittery = (np.array([0.0, 1.0, 2.3, 3.0, 3.75, 5.0]) * STEP).astype(np.int64)
    assert rate_map(jittery, STEP, 1e6) == list(range(6))  # jitter under half a frame moves nothing


def test_keep_is_the_dropped_frame_filling(tmp_path: Path, make_mcraw: Callable[..., Path]) -> None:
    clip_path = make_mcraw(tmp_path / "clip.mcraw", [i * STEP for i in (0, 1, 2, 4, 5)])
    with Clip(clip_path) as clip:
        assert clip_sources(clip, True) == [0, 1, 2, 2, 3, 4]
        assert clip_sources(clip, True, "keep", 20.0) == [0, 1, 2, 2, 3, 4]
    facts = read_clip_facts(clip_path, build_recipe([{}]))
    assert facts.target is None
    assert facts.tempo == pytest.approx(float(facts.rate) / facts.measured_fps)


def test_a_dng_export_at_20_fps(tmp_path: Path, make_mcraw: Callable[..., Path]) -> None:
    times = [i * STEP for i in range(24)]  # one second at 24 fps
    audio = np.zeros((48000, 2), np.int16)
    clip = make_mcraw(tmp_path / "clip.mcraw", times, audio=audio)
    recipe = build_recipe([{"output": {"kind": "dng", "frame_rate": 20}}])
    facts = read_clip_facts(clip, recipe)
    assert facts.target == Fraction(20)
    assert facts.tempo == 1.0
    result = export_dng(clip, tmp_path / "clip_DNG", recipe, workers=1)
    assert result.frames == 20  # the recording's length (24 frames of 1/24 s) at 20 fps
    tags = read_dng_tags(min(result.output.glob("*.dng")))
    assert tags[51044] == pytest.approx((20.0,))
    with wave.open(str(result.output / "clip.wav")) as wav:
        assert wav.getnframes() == round(result.frames / 20 * 48000)  # sound at its recorded speed
    sidecar = tomllib.loads(result.sidecar.read_text())
    assert sidecar["measurements"]["frame_rate_target"] == "20"
    assert any(note.startswith("converted to 20 fps: ") for note in result.warnings)


def test_ranges_count_converted_frames(tmp_path: Path, make_mcraw: Callable[..., Path]) -> None:
    clip = make_mcraw(tmp_path / "clip.mcraw", [i * STEP for i in range(24)])
    span, _ = frame_range_for(clip, "10f", None, True, frame_rate=12.0)
    assert span == (10, 2)  # 12 frames at 12 fps from 0 to 0.958 s
    plain, _ = frame_range_for(clip, "10f", None, True)
    assert plain == (10, 14)


def test_settings_without_effect_are_noted() -> None:
    mcraw = build_recipe([{"output": {"kind": "mcraw", "frame_rate": "integer"}}])
    assert (
        "output.frame_rate has no effect on MCRAW output: it copies the recorded frames"
        in ignored_settings(mcraw)
    )
    unfilled = build_recipe([{"output": {"kind": "dng", "frame_rate": "integer", "fill_dropped": False}}])
    assert "output.fill_dropped has no effect with a frame-rate conversion" in ignored_settings(unfilled)


def test_frame_rate_choices() -> None:
    assert build_recipe([{}]).output.frame_rate == "keep"
    assert build_recipe([{"output": {"frame_rate": "25"}}]).output.frame_rate == 25.0
    assert build_recipe([{"output": {"frame_rate": "drop-frame"}}]).output.frame_rate == "drop-frame"
