import dataclasses
from pathlib import Path

import numpy as np
import pytest

from mcrawsome.colour.spaces import AP1_TO_DAVINCI_WG
from mcrawsome.colour.transfer import acescct_decode, davinci_intermediate_decode
from mcrawsome.develop.raw import Image
from mcrawsome.io.clip import Clip
from mcrawsome.pipeline.render import FrameJob, choose_neutral, iter_frames, render_frame


def _job(path: Path, **kwargs: object) -> FrameJob:
    with Clip(path) as clip:
        neutral = choose_neutral(clip, "median")
    return FrameJob(clip_path=path, exposure_stops=0.0, neutral=neutral, **kwargs)  # type: ignore[arg-type]


@pytest.mark.fixture
def test_render_frame_shape_and_range(fixture_12bit: Path) -> None:
    out = render_frame(_job(fixture_12bit), 0)
    assert out.shape == (2160, 3840, 3)
    assert out.dtype == np.uint16
    assert 15_000 < int(np.median(out)) < 40_000  # plausible ACEScct mid-tones


@pytest.mark.fixture
def test_iterator_yields_every_frame(fixture_12bit: Path) -> None:
    frames = list(iter_frames(_job(fixture_12bit), frame_count=3, workers=2))
    assert len(frames) == 3


@pytest.mark.fixture
def test_davinci_target_decodes_to_the_same_light(fixture_12bit: Path) -> None:
    aces = render_frame(_job(fixture_12bit), 0)[::16, ::16].reshape(-1, 3) / 65535.0
    dwg = render_frame(_job(fixture_12bit, target="davinci"), 0)[::16, ::16].reshape(-1, 3) / 65535.0
    from_aces = acescct_decode(aces) @ AP1_TO_DAVINCI_WG.T
    from_dwg = davinci_intermediate_decode(dwg)
    mid = (from_aces.mean(1) > 0.02) & (from_aces.mean(1) < 2)
    np.testing.assert_allclose(from_dwg[mid], from_aces[mid], rtol=2e-3, atol=2e-4)


@pytest.mark.fixture
def test_identity_plan_changes_almost_nothing(fixture_12bit: Path, tmp_path: Path) -> None:
    from mcrawsome.motion.plan import StabilisationPlan

    plain = render_frame(_job(fixture_12bit), 1)
    h, w = plain.shape[:2]
    identity = np.broadcast_to(np.eye(3, dtype=np.float32), (3, 8, 3, 3)).copy()
    plan = StabilisationPlan(identity, np.tile([1.0, 0, 0, 0], (3, 1)), 3000.0, 1.0, w, h, 0, 0.0, 0.0, 1.0)
    path = tmp_path / "plan.npz"
    plan.save(path)
    warped = render_frame(_job(fixture_12bit, plan_path=path), 1)
    assert warped.shape == plain.shape
    assert np.abs(warped.astype(np.int32) - plain).mean() < 2 * 16  # under 2 codes in 12-bit terms


@pytest.mark.fixture
def test_sampled_frames_come_back_once_per_index(fixture_12bit: Path) -> None:
    frames = list(iter_frames(_job(fixture_12bit), frame_count=3, workers=2, indices=[0, 2]))
    assert len(frames) == 2


def _recipe_job(path: Path, **recipe: object) -> tuple[FrameJob, int]:
    from mcrawsome.output.export import frame_job, read_clip_facts
    from mcrawsome.recipe.load import build_recipe

    built = build_recipe([recipe])
    facts = read_clip_facts(path, built)
    return frame_job(path, built, facts), facts.frame_count


@pytest.mark.fixture
@pytest.mark.slow
def test_raw_ranges_match_the_full_render(fixture_10bit_rotated: Path) -> None:
    job = _job(fixture_10bit_rotated)
    full = list(iter_frames(job, frame_count=3, workers=2))
    emitted = [
        f for start, count in ((0, 1), (1, 2)) for f in iter_frames(job, 3, workers=2, emit=(start, count))
    ]
    trimmed = list(iter_frames(dataclasses.replace(job, first_frame=1, clip_frames=3), 2, workers=2))
    assert len(emitted) == 3
    assert len(trimmed) == 2
    for a, b in zip(full, emitted, strict=True):
        np.testing.assert_array_equal(a, b)
    for a, b in zip(full[1:], trimmed, strict=True):
        np.testing.assert_array_equal(a, b)


@pytest.mark.fixture
def test_frame_job_knows_the_clip_length(fixture_12bit: Path) -> None:
    job, count = _recipe_job(fixture_12bit)
    assert job.clip_frames == count == 3


@pytest.mark.parametrize("indices", [None, [1, 2, 10, 14]])
@pytest.mark.parametrize(
    "clip_type", ["synthetic", pytest.param("fixture", marks=[pytest.mark.fixture, pytest.mark.slow])]
)
def test_development_matches_requested_source_units(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    indices: list[int] | None,
    request: pytest.FixtureRequest,
    clip_type: str,
) -> None:
    from collections import Counter
    from multiprocessing.pool import ThreadPool

    from conftest import SYNTHETIC_STEP, write_mcraw

    from mcrawsome.pipeline import render, shared
    from mcrawsome.pipeline.units import plan_units

    count = 3 if clip_type == "fixture" else 15
    path = (
        request.getfixturevalue("fixture_10bit_rotated")
        if clip_type == "fixture"
        else write_mcraw(tmp_path / "tiny.mcraw", [i * SYNTHETIC_STEP for i in range(count)])
    )
    sources = (0, 1, 1, *range(2, count))
    job = _job(path, sources=sources)
    developed: list[int] = []
    original = render._develop_raw

    def spy(job: FrameJob, index: int) -> Image:
        developed.append(index)
        return original(job, index)

    # One thread keeps Clip's file reads serial and makes the develop spy visible in the parent.
    monkeypatch.setattr(render, "worker_pool", lambda workers: ThreadPool(1))
    monkeypatch.setattr(render, "_develop_raw", spy)
    attached = set(shared._attached)
    try:
        list(iter_frames(job, len(sources), workers=2, indices=indices))
    finally:  # thread workers attach in this process, so release their mappings here
        for name in shared._attached.keys() - attached:
            shared._attached.pop(name).close()
    needed = [
        frame for unit in plan_units(len(sources), 0, 1, indices, sources=sources) for frame in unit.frames
    ]
    assert Counter(developed) == Counter(needed)
