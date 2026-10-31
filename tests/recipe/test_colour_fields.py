"""The Recipe's colour fields: input gamut and transfer, log output in any pair."""

import pytest

from mcrawsome.recipe.load import RecipeError, build_recipe
from mcrawsome.recipe.schema import Recipe


def test_input_gamut_and_transfer_take_registry_ids() -> None:
    recipe = build_recipe([{"develop": {"input_gamut": "s-gamut3-cine", "input_transfer": "s-log3"}}])
    assert (recipe.develop.input_gamut, recipe.develop.input_transfer) == ("s-gamut3-cine", "s-log3")
    with pytest.raises(RecipeError, match=r"develop\.input_gamut"):
        build_recipe([{"develop": {"input_gamut": "nowhere"}}])


def test_the_legacy_input_encoding_cannot_be_mixed_with_the_pair() -> None:
    with pytest.raises(RecipeError, match=r"develop\.input_encoding or develop\.input_gamut"):
        build_recipe([{"develop": {"input_encoding": "davinci", "input_transfer": "s-log3"}}])


@pytest.mark.parametrize("table", [{"develop": {"input_ei": 900}}])
def test_an_unknown_exposure_index_is_refused(table: dict[str, dict[str, int]]) -> None:
    with pytest.raises(RecipeError, match="EI"):
        build_recipe([table])


def test_an_exposure_index_needs_logc3() -> None:
    with pytest.raises(RecipeError, match=r"develop\.input_ei needs a LogC3 input"):
        build_recipe([{"develop": {"input_transfer": "s-log3", "input_ei": 1600}}])


def test_camera_native_videos_are_refused_by_default_for_graded_output() -> None:
    assert Recipe().develop.native_as == "unset"
    assert build_recipe([{"develop": {"native_as": "ap1"}}]).develop.native_as == "ap1"
