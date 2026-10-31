import math

import pytest
from hypothesis import given
from hypothesis import strategies as st

from mcrawsome.analysis.measure import SampleStats
from mcrawsome.analysis.resolve import TARGET_KEY, auto_exposure
from mcrawsome.recipe.load import RecipeError, build_recipe, parse_override


def _stats(key: float) -> SampleStats:
    return SampleStats(key=key, clipped_share=0.0, noise_rel=0.01, noise_level=0.1, frames=24)


def test_a_dim_clip_is_pushed_up() -> None:
    stops, note = auto_exposure(_stats(TARGET_KEY / 4))
    assert stops == 2.0
    assert note is None


def test_black_frames_get_the_largest_push_and_a_note() -> None:
    stops, note = auto_exposure(_stats(0.0))
    assert stops == 3.0
    assert note is not None


def test_a_very_bright_clip_is_held_at_the_limit_with_a_note() -> None:
    stops, note = auto_exposure(_stats(TARGET_KEY * 32))
    assert stops == -2.0
    assert note is not None


@given(st.floats(0, 100, allow_nan=False))
def test_auto_exposure_is_finite_bounded_and_on_a_tenth_stop_grid(key: float) -> None:
    stops, _ = auto_exposure(_stats(key))
    assert math.isfinite(stops)
    assert -2.0 <= stops <= 3.0
    assert round(stops * 10) == pytest.approx(stops * 10, abs=1e-9)


def test_the_recipe_takes_auto_or_a_number() -> None:
    assert build_recipe([]).develop.exposure == "auto"
    assert build_recipe([{"develop": {"exposure": 1.5}}]).develop.exposure == 1.5
    assert build_recipe([parse_override("develop.exposure=auto")]).develop.exposure == "auto"
    assert build_recipe([parse_override("develop.exposure=-1")]).develop.exposure == -1
    with pytest.raises(RecipeError, match="exposure"):
        build_recipe([parse_override("develop.exposure=bright")])


@pytest.mark.local
@pytest.mark.parametrize(
    ("stem", "chosen"),
    [("261006_005913_VIDEO_25mm", 2.5), ("261008_095010_VIDEO_25mm", 1.5), ("251011_153704_VIDEO_25mm", 1.0)],
)
def test_auto_exposure_matches_the_owners_choices(stem: str, chosen: float) -> None:
    from conftest import LOCAL_DIR

    from mcrawsome.analysis.measure import measure_samples
    from mcrawsome.output.export import frame_job, read_clip_facts

    path = LOCAL_DIR / f"{stem}.mcraw"
    if not path.exists():
        pytest.skip(f"local clip {path.name} not present")
    recipe = build_recipe([])
    facts = read_clip_facts(path, recipe)
    stops, _ = auto_exposure(measure_samples(frame_job(path, recipe, facts), facts.frame_count, 4))
    assert stops == pytest.approx(chosen, abs=0.5)
