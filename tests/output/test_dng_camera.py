"""DNG camera model and colour matrix overrides."""

import json
from collections.abc import Callable
from pathlib import Path

import numpy as np
import pytest

from mcrawsome.camera.colour import camera_to_ap1, with_colour_matrix
from mcrawsome.io.metadata import parse_container_metadata
from mcrawsome.output.dng import camera_model, read_dng_tags
from mcrawsome.output.dng_export import export_dng
from mcrawsome.recipe.load import RecipeError, build_recipe, parse_override

DATA = Path(__file__).parent.parent / "data"
MATRIX = (0.9, -0.3, -0.1, -0.4, 1.3, 0.1, -0.05, 0.2, 0.6)


def test_camera_model_names() -> None:
    assert camera_model("Pixel 7", "clip", None, False) == ("MotionCam", "Pixel 7", "MotionCam Pixel 7")
    assert camera_model("Pixel 7", "clip", None, True) == (
        "MotionCam", "Pixel 7 clip", "MotionCam Pixel 7 clip",
    )  # fmt: skip
    assert camera_model("Pixel 7", "clip", "Panasonic Varicam RAW", False) == (
        "Panasonic", "Varicam RAW", "Panasonic Varicam RAW",
    )  # fmt: skip
    assert camera_model("Pixel 7", "clip", "Blackmagic Pocket Cinema Camera 4K", False) == (
        "Blackmagic Design", "Pocket Cinema Camera 4K", "Blackmagic Pocket Cinema Camera 4K",
    )  # fmt: skip
    assert camera_model("Pixel 7", "clip", "My Camera", True) == (
        "MotionCam",
        "My Camera clip",
        "My Camera clip",
    )


def test_the_colour_matrix_override_maps_the_neutral_to_white() -> None:
    meta = parse_container_metadata(json.loads((DATA / "container_metadata_pixel10.json").read_text()))
    custom = with_colour_matrix(meta, MATRIX)
    assert custom.colour_matrix_1 == custom.colour_matrix_2 == list(MATRIX)
    np.testing.assert_allclose(
        np.array(custom.forward_matrix_1).reshape(3, 3),
        np.linalg.inv(np.array(MATRIX).reshape(3, 3)),
        atol=1e-12,
    )
    neutral = (0.5, 1.0, 0.7)
    matrix, _ = camera_to_ap1(custom, neutral)
    white = matrix @ np.ones(3, np.float32)  # white-balanced camera white
    np.testing.assert_allclose(white / white[1], [1.0, 1.0, 1.0], atol=1e-3)
    plain, _ = camera_to_ap1(meta, neutral)
    assert not np.allclose(plain, matrix)


def test_the_colour_matrix_reads_nine_values() -> None:
    camera = build_recipe([parse_override("camera.colour_matrix=" + ",".join(map(str, MATRIX)))]).camera
    assert camera.colour_matrix == MATRIX
    with pytest.raises(RecipeError) as caught:
        build_recipe([{"camera": {"colour_matrix": [1, 0, 0]}}])
    assert caught.value.field == "camera.colour_matrix"


def test_default_dng_names_and_matrices(tmp_path: Path, make_mcraw: Callable[..., Path]) -> None:
    clip = make_mcraw(tmp_path / "clip.mcraw", [0, 41_666_667])
    result = export_dng(clip, tmp_path / "plain_DNG", build_recipe([{"output": {"kind": "dng"}}]), workers=1)
    tags = read_dng_tags(min(result.output.glob("*.dng")))
    assert tags[271] == "MotionCam"
    assert tags[50708] == f"MotionCam {tags[272]}"
    assert 50964 in tags
    assert 50965 in tags


def test_overridden_dng_names_and_matrices(tmp_path: Path, make_mcraw: Callable[..., Path]) -> None:
    clip = make_mcraw(tmp_path / "clip.mcraw", [0, 41_666_667])
    recipe = build_recipe(
        [
            {
                "output": {
                    "kind": "dng",
                    "dng_camera_model": "Panasonic Varicam RAW",
                    "dng_unique_model": True,
                },
                "camera": {"colour_matrix": list(MATRIX)},
            }
        ]
    )
    result = export_dng(clip, tmp_path / "clip_DNG", recipe, workers=1)
    tags = read_dng_tags(min(result.output.glob("*.dng")))
    assert (tags[271], tags[272], tags[50708]) == (
        "Panasonic",
        "Varicam RAW clip",
        "Panasonic Varicam RAW clip",
    )
    assert tags[50721] == pytest.approx(MATRIX, abs=1e-6)
    assert tags[50722] == pytest.approx(MATRIX, abs=1e-6)
    assert 50964 not in tags
    assert 50965 not in tags


def test_frame_jobs_carry_the_colour_matrix(tmp_path: Path, make_mcraw: Callable[..., Path]) -> None:
    from mcrawsome.output.export import frame_job, read_clip_facts

    clip = make_mcraw(tmp_path / "clip.mcraw", [0, 41_666_667])
    recipe = build_recipe([{"camera": {"colour_matrix": list(MATRIX)}}])
    assert frame_job(clip, recipe, read_clip_facts(clip, recipe)).colour_matrix == MATRIX


@pytest.mark.parametrize("unique", [False, True])
@pytest.mark.parametrize(
    ("preset", "offset"),
    [("Panasonic Varicam RAW", -2), ("Blackmagic Pocket Cinema Camera 4K", 0)],
)
def test_export_camera_presets_match_mounted_identity_and_exposure(
    tmp_path: Path, make_mcraw: Callable[..., Path], preset: str, offset: int, unique: bool
) -> None:
    from mcrawsome.mount.sequence import Sequence
    from mcrawsome.recipe.raw import RawCalibration

    clip = make_mcraw(tmp_path / "clip.mcraw", [0, 41_666_667])
    recipe = build_recipe(
        [
            {
                "develop": {"exposure": 0},
                "output": {
                    "kind": "dng",
                    "dng_camera_model": preset,
                    "dng_unique_model": unique,
                },
            }
        ]
    )
    result = export_dng(clip, tmp_path / "dng", recipe, workers=1)
    tags = read_dng_tags(min(result.output.glob("*.dng")))
    with Sequence(
        clip,
        raw_settings=RawCalibration(
            mount_camera_model=preset,
            mount_unique_model=unique,
        ),
    ) as mounted:
        info = mounted.frame_info(0)
        assert tags[50708] == info.unique_model
        assert tags[50730][0] == pytest.approx(info.baseline_exposure)
        assert tags[50730][0] == pytest.approx(offset)
        assert tags[50721] == pytest.approx(info.colour_matrix_1, abs=1e-6)
        assert tags[50728] == pytest.approx(info.as_shot_neutral, abs=1e-6)
