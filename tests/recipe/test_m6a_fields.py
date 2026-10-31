"""Milestone 6 part A's Recipe settings: parsing from TOML lists and command-line text, and their checks."""

import pytest

from mcrawsome.recipe.load import RecipeError, build_recipe, parse_override


def test_black_level_reads_one_value_or_a_comma_list() -> None:
    assert build_recipe([{"develop": {"black_level": 64}}]).develop.black_level == 64.0
    four = build_recipe([{"develop": {"black_level": "60, 61,62,63"}}]).develop.black_level
    assert four == (60.0, 61.0, 62.0, 63.0)
    assert build_recipe([parse_override("develop.black_level=64,64,64,66")]).develop.black_level == (
        64.0, 64.0, 64.0, 66.0,
    )  # fmt: skip


def test_black_level_needs_one_or_four_values() -> None:
    with pytest.raises(RecipeError) as caught:
        build_recipe([{"develop": {"black_level": [1, 2, 3]}}])
    assert caught.value.field == "develop.black_level"


def test_levels_default_to_the_clips() -> None:
    develop = build_recipe([{}]).develop
    assert develop.black_level is None
    assert develop.white_level is None


@pytest.mark.parametrize("rate", [0.5, 1e-10, float("nan"), float("inf")])
def test_custom_frame_rates_stay_in_the_planned_range(rate: float) -> None:
    with pytest.raises(RecipeError):
        build_recipe([{"output": {"frame_rate": rate}}])


@pytest.mark.parametrize("matrix", [[0.0] * 9, [float("nan")] * 9, [float("inf")] * 9])
def test_colour_matrices_must_be_finite_and_invertible(matrix: list[float]) -> None:
    with pytest.raises(RecipeError) as caught:
        build_recipe([{"camera": {"colour_matrix": matrix}}])
    assert caught.value.field == "camera.colour_matrix"
