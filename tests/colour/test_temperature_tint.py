"""Develop controls invert the same DNG maths used by native bin defaults."""

from dataclasses import replace
from fractions import Fraction
from pathlib import Path

import numpy as np
import pytest

from mcrawsome.colour.white_balance import neutral_for_temperature
from mcrawsome.io.clip import Clip
from mcrawsome.output.dng import dng_info
from mcrawsome.recipe.load import RecipeError, build_recipe
from mcrawsome.resolve.bin_white_balance import white_balance


@pytest.mark.parametrize("kelvin", [2000, 2500, 2850, 4000, 5000, 6500, 8000, 10000, 12000])
@pytest.mark.parametrize("tint", [-100, -50, 0, 50, 100])
def test_temperature_and_tint_round_trip(tiny_raw: Path, kelvin: float, tint: float) -> None:
    with Clip(tiny_raw) as clip:
        info = dng_info(clip.metadata, clip.frame_metadata(0), Fraction(24), 0, 0)
    # The approved camera cannot represent these combinations, including positive responses whose
    # intermediate NeutralToXY iteration is rejected by the existing SDK-compatible forward path.
    if (kelvin, tint) in {(2000, 50), (2500, 50), (2000, 100), (2500, 100), (2850, 100)}:
        with pytest.raises(ValueError, match=r"outside what this camera.*represent"):
            neutral_for_temperature(info, kelvin, tint)
        return
    neutral = neutral_for_temperature(info, kelvin, tint)
    assert max(neutral) == 1
    assert min(neutral) > 0
    actual = white_balance(replace(info, as_shot_neutral=neutral))
    assert actual[0] == pytest.approx(kelvin, abs=1)
    assert actual[1] == pytest.approx(tint, abs=0.1)


def test_unrepresentable_white_balance_is_a_clip_specific_recipe_error(tiny_raw: Path) -> None:
    from mcrawsome.output.export import ExportError, read_clip_facts

    recipe = build_recipe([{"develop": {"white_balance": "custom", "temperature": 2000, "tint": 100}}])
    with pytest.raises(ExportError, match=r"2000 K with tint \+100.*outside what this camera") as caught:
        read_clip_facts(tiny_raw, recipe, audio=False)
    assert caught.value.code == "recipe-invalid"


def test_export_resolution_checks_the_camera(tiny_raw: Path) -> None:
    from mcrawsome.output.plan import resolve_recipe

    with pytest.raises(RecipeError, match="outside what this camera"):
        resolve_recipe(
            tiny_raw,
            [],
            None,
            {},
            (
                "develop.white_balance=custom",
                "develop.temperature=2000",
                "develop.tint=100",
            ),
        )


def test_as_shot_is_accepted_without_custom_range_limits(tiny_raw: Path) -> None:
    from mcrawsome.output.export import read_clip_facts

    with Clip(tiny_raw) as clip:
        recorded = clip.frame_metadata(0).as_shot_neutral
    assert read_clip_facts(tiny_raw, build_recipe([]), audio=False).neutral == recorded


@pytest.mark.parametrize(
    ("kelvin", "tint"), [(1999, 0), (12001, 0), (5000, -101), (5000, 101), (np.nan, 0), (5000, np.inf)]
)
def test_temperature_range_is_plain(tiny_raw: Path, kelvin: float, tint: float) -> None:
    with Clip(tiny_raw) as clip:
        info = dng_info(clip.metadata, clip.frame_metadata(0), Fraction(24), 0, 0)
    with pytest.raises(ValueError, match=r"2000.*12000|-100.*100"):
        neutral_for_temperature(info, kelvin, tint)


def test_old_recipe_and_custom_recipe() -> None:
    assert build_recipe([{"develop": {"white_balance": "median"}}]).develop.white_balance == "median"
    assert build_recipe([{}]).develop.white_balance == "median"
    custom = build_recipe([{"develop": {"white_balance": "custom", "temperature": 6000, "tint": 12}}])
    assert custom.develop.temperature == 6000
    assert custom.develop.tint == 12


@pytest.mark.parametrize(
    "develop",
    [
        {"white_balance": "custom"},
        {"white_balance": "custom", "temperature": 5000},
        {"white_balance": "custom", "tint": 0},
        {"temperature": 12001},
        {"tint": -101},
        {"temperature": float("nan")},
    ],
)
def test_custom_recipe_requires_both_values_in_range(develop: dict[str, object]) -> None:
    with pytest.raises(RecipeError):
        build_recipe([{"develop": develop}])
