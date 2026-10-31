from pathlib import Path

import pytest
from click.testing import CliRunner

from mcrawsome.cli.main import cli
from mcrawsome.recipe.load import (
    RecipeError,
    build_recipe,
    default_layer_paths,
    parse_override,
    recipe_fields,
    recipe_to_toml,
)


def test_misspelt_key_gets_a_suggestion() -> None:
    with pytest.raises(RecipeError) as err:
        build_recipe([{"develop": {"exposre": 1.0}}])
    message = str(err.value)
    assert "develop.exposre" in message
    assert "develop.exposure" in message


def test_out_of_range_value_names_the_range() -> None:
    with pytest.raises(RecipeError, match=r"develop\.lens_shading"):
        build_recipe([{"develop": {"lens_shading": -1}}])


def test_parse_override() -> None:
    assert parse_override("develop.exposure=1.5") == {"develop": {"exposure": 1.5}}
    assert parse_override("look=gorgeous") == {"look": "gorgeous"}
    with pytest.raises(RecipeError, match="key=value"):
        parse_override("nonsense")


def test_every_leaf_field_is_listed_once() -> None:
    names = [name for name, _, _ in recipe_fields()]
    assert "develop.exposure" in names
    assert "output.dng_scale" in names
    assert not any(name.startswith(("denoise.", "grade.", "effects.")) for name in names)
    assert "output.crf" not in names
    assert len(names) == len(set(names))


def test_toml_round_trip(tmp_path: Path) -> None:
    import tomllib

    r = build_recipe([{"output": {"crf": 10}}])
    assert build_recipe([tomllib.loads(recipe_to_toml(r))]) == r


def test_default_layers_only_include_existing_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    clip = tmp_path / "clips" / "a.mcraw"
    clip.parent.mkdir()
    assert default_layer_paths(clip) == []
    (clip.parent / "mcrawsome.toml").write_text("look = 'gorgeous'\n")
    assert default_layer_paths(clip) == [clip.parent / "mcrawsome.toml"]


# --- Milestone 2: HDR, effects, grade, output kinds ---------------------------------------------------


def test_unknown_output_kind() -> None:
    with pytest.raises(RecipeError, match=r"output\.kind"):
        build_recipe([{"output": {"kind": "gif"}}])


def test_recipe_with_unset_overrides_writes_toml() -> None:
    text = recipe_to_toml(build_recipe([{"develop": {"exposure": 0.5}}]))
    assert "exposure = 0.5" in text
    assert "vignette" not in text  # unset overrides are left out


def test_generated_options_reach_the_recipe(tmp_path: Path) -> None:
    clip = tmp_path / "a.mcraw"
    clip.write_bytes(b"")
    result = CliRunner().invoke(
        cli, ["export", str(clip), "--dry-run", "--develop.exposure", "0.5", "--output.dng_scale", "half"]
    )
    assert result.exit_code == 0, result.output
    assert "exposure = 0.5" in result.output
    assert 'dng_scale = "half"' in result.output


def test_motion_defaults_and_ranges() -> None:
    r = build_recipe([])
    assert r.motion.stabilise == "auto"
    assert r.motion.crop == 1.10
    with pytest.raises(RecipeError, match=r"motion\.crop"):
        build_recipe([{"motion": {"crop": 2.5}}])
    with pytest.raises(RecipeError, match=r"motion\.crop"):
        build_recipe([{"motion": {"crop": 0.9}}])
    # With no margin nothing can be corrected; crops saved from 1.0 before the minimum load at it.
    assert build_recipe([{"motion": {"crop": 1.0}}]).motion.crop == 1.05
    assert build_recipe([{"motion": {"crop": 1.07}}]).motion.crop == 1.07
    with pytest.raises(RecipeError, match=r"motion\.stabilise"):
        build_recipe([{"motion": {"stabilise": "maybe"}}])


def test_saved_stabilisation_is_accepted_for_raw_outputs() -> None:
    recipe = build_recipe([{"motion": {"stabilise": "on"}, "output": {"kind": "dng"}}])
    assert recipe.motion.stabilise == "on"


def test_crop_alias_sets_motion_crop(tmp_path: Path) -> None:
    clip = tmp_path / "a.mcraw"
    clip.write_bytes(b"x")
    result = CliRunner().invoke(
        cli, ["export", str(clip), "--dry-run", "--motion.crop", "1.2", "--motion.stabilise", "on"]
    )
    assert result.exit_code == 0, result.output
    assert "crop = 1.2" in result.output
    assert 'stabilise = "on"' in result.output


def test_highlight_recovery_strength_is_a_fraction() -> None:
    assert build_recipe([{"develop": {"highlight_recovery": 0.5}}]).develop.highlight_recovery == 0.5
    assert build_recipe([]).develop.highlight_recovery == 1.0
    with pytest.raises(RecipeError, match="highlight_recovery"):
        build_recipe([{"develop": {"highlight_recovery": 1.5}}])


def test_lens_shading_strength_is_a_fraction() -> None:
    assert build_recipe([]).develop.lens_shading == 1.0
    assert build_recipe([{"develop": {"lens_shading": 0.0}}]).develop.lens_shading == 0.0
    with pytest.raises(RecipeError, match="lens_shading"):
        build_recipe([{"develop": {"lens_shading": -0.1}}])


def test_legacy_creative_settings_are_removed_without_mutating_saved_layers() -> None:
    from copy import deepcopy

    layer = {
        "look": "gritty",
        "hdr": True,
        "grade": {"sat": 0.2},
        "effects": {"grain": 0.5},
        "denoise": {"temporal_radius": 2},
        "output": {"kind": "exr", "crf": 12, "log": "davinci", "exr_compression": "piz"},
        "develop": {"exposure": 1.25},
    }
    before = deepcopy(layer)
    recipe = build_recipe([layer])
    assert layer == before
    assert recipe.develop.exposure == 1.25
    assert recipe.output.exr_compression == "piz"
    saved = recipe.model_dump()
    assert not {"look", "hdr", "grade", "effects", "denoise"}.intersection(saved)
    assert not {"crf", "log"}.intersection(saved["output"])


@pytest.mark.parametrize("kind", ["av1", "prores", "dnxhr", "h264", "hevc", "vp9", "apv", "hdr-sdr"])
def test_saved_video_export_requests_explain_the_new_scope(kind: str) -> None:
    with pytest.raises(RecipeError, match="no longer encodes video"):
        build_recipe([{"output": {"kind": kind}}])
