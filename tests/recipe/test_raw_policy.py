"""Policy persistence and pixel/tag oracles using independent numerical oracles."""

from pathlib import Path

import numpy as np
import pytest

from mcrawsome.develop.raw import Developer
from mcrawsome.io.clip import Clip
from mcrawsome.io.metadata import FrameMetadata
from mcrawsome.mount.sequence import Sequence
from mcrawsome.output.export import frame_job, read_clip_facts
from mcrawsome.recipe.clip_layer import read_layer, write_layer
from mcrawsome.recipe.defaults import apply_defaults
from mcrawsome.recipe.load import build_recipe, recipe_to_toml
from mcrawsome.recipe.raw import RawCalibration


@pytest.fixture
def changing_levels(monkeypatch: pytest.MonkeyPatch) -> None:
    original = Clip.frame_metadata

    def metadata(clip: Clip, index: int) -> FrameMetadata:
        return original(clip, index).model_copy(
            update={
                "black_level": (64.0 + index, 65.0 + index, 66.0 + index, 67.0 + index),
                "white_level": 4095 - 100 * index,
            }
        )

    monkeypatch.setattr(Clip, "frame_metadata", metadata)


def test_recorded_over_fixed_defaults_survives_save_reload_and_reset(
    tiny_raw: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changing_levels: None
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    fixed = {"develop": {"black_level": 42, "white_level": 3000}}
    defaults = apply_defaults(fixed, "")
    saved = write_layer(tiny_raw, {"develop": {"level_policy": "recorded"}}, "")
    loaded = read_layer(tiny_raw)
    assert loaded == saved
    recipe = build_recipe([defaults.values, loaded.values])
    # The serialised marker, rather than a null tombstone, bypasses both inherited levels.
    assert 'level_policy = "recorded"' in recipe_to_toml(recipe)
    with Sequence(tiny_raw, raw_settings=recipe.develop) as dynamic:
        for i in (0, 2, 1):
            info = dynamic.frame_info(i)
            assert info.black_levels == tuple(64.0 + site + i for site in range(4))
            assert info.white_level == 4095 - 100 * i
        old_name = dynamic.names()[0]
        old_bytes = dynamic.read(old_name, 0, dynamic.size(old_name))
        write_layer(tiny_raw, {}, saved.version)
        inherited = build_recipe([defaults.values, read_layer(tiny_raw).values])
        with Sequence(tiny_raw, raw_settings=inherited.develop) as reset:
            assert reset.frame_info(2).black_levels == (42.0,) * 4
            assert reset.frame_info(2).white_level == 3000
        assert dynamic.read(old_name, 0, dynamic.size(old_name)) == old_bytes


@pytest.mark.parametrize("policy", ["legacy", "explicit", "recorded", "static"])
def test_policy_has_one_meaning_in_frame_jobs_and_dngs(
    tiny_raw: Path, changing_levels: None, policy: str
) -> None:
    recipe = build_recipe(
        [{"develop": {"black_level": [1, 2, 3, 4], "white_level": 2000, "level_policy": policy}}]
    )
    job = frame_job(tiny_raw, recipe, read_clip_facts(tiny_raw, recipe))
    with Sequence(tiny_raw, raw_settings=recipe.develop) as sequence:
        first, last = sequence.frame_info(0), sequence.frame_info(2)
        if policy in ("legacy", "explicit"):
            assert first.black_levels == last.black_levels == (1, 2, 3, 4)
            assert job.black_level == (1, 2, 3, 4)
        elif policy == "static":
            assert first.black_levels == last.black_levels == (64, 65, 66, 67)
            assert first.white_level == last.white_level == 4095
            assert job.black_level == (64, 65, 66, 67)
        else:
            assert first.black_levels != last.black_levels
            assert job.black_level is job.white_level is None
        assert job.dynamic_levels == (policy == "recorded")


def test_legacy_partial_overrides_keep_precedence_over_new_recorded_defaults() -> None:
    recipe = build_recipe(
        [
            {
                "develop": {
                    "level_policy": "recorded",
                    "mount_camera_model": "Default camera",
                    "camera_model_source": "recorded",
                }
            },
            {"develop": {"black_level": 12, "mount_camera_model": "Clip camera"}},
        ]
    )
    assert recipe.develop.level_policy == "legacy"
    assert recipe.develop.black_level == 12
    assert recipe.develop.white_level is None
    assert recipe.develop.camera_model_source == "override"
    assert recipe.develop.mount_camera_model == "Clip camera"


def test_recorded_camera_bypasses_fixed_name_without_changing_old_media(tiny_raw: Path) -> None:
    settings = RawCalibration(mount_camera_model="Panasonic Varicam RAW")
    with (
        Sequence(tiny_raw, raw_settings=settings) as fixed,
        Sequence(
            tiny_raw, raw_settings=settings.model_copy(update={"camera_model_source": "recorded"})
        ) as recorded,
        Sequence(tiny_raw) as plain,
    ):
        assert fixed.frame_info(0).model == "Varicam RAW"
        assert recorded.frame_info(0).model == plain.frame_info(0).model
        assert fixed.frame_info(0).model == "Varicam RAW"


def test_dynamic_development_matches_independent_site_normalisation(tiny_raw: Path) -> None:
    with Clip(tiny_raw) as clip:
        first = clip.frame_metadata(0).model_copy(
            update={"black_level": (64.0, 65.0, 66.0, 67.0), "white_level": 4095}
        )
        later = first.model_copy(update={"black_level": (100.0, 110.0, 120.0, 130.0), "white_level": 2000})
        developer = Developer(clip.metadata, first, (1.0, 1.0, 1.0), lens_shading=0)
        changed = developer.with_levels(later)
        raw = np.full((first.height, first.width), 500, np.uint16)
        for i, (y, x) in enumerate(((0, 0), (0, 1), (1, 0), (1, 1))):
            expected = (500 - later.black_level[i]) / (later.white_level - later.black_level[i])
            actual = (raw[y::2, x::2] - changed.black[y::2, x::2]) * changed.gain[y::2, x::2]
            np.testing.assert_allclose(actual, expected, rtol=1e-6)
        assert changed.white == 2000
        assert developer.white == 4095
        assert changed.with_levels(later) is changed
        np.testing.assert_allclose(
            changed.develop_half(raw),
            Developer(clip.metadata, later, (1.0, 1.0, 1.0), lens_shading=0).develop_half(raw),
            atol=1e-6,
        )


def test_constant_fractional_levels_reuse_developer(tiny_raw: Path) -> None:
    with Clip(tiny_raw) as clip:
        frame = clip.frame_metadata(0).model_copy(update={"black_level": (64.1, 64.2, 64.3, 64.4)})
        developer = Developer(clip.metadata, frame, (1.0, 1.0, 1.0))
        assert developer.with_levels(frame) is developer
