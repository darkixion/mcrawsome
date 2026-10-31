"""Black-edge crop: finding a black strip on an edge, custom sizes, and the shading map kept aligned."""

import json
from collections.abc import Callable
from pathlib import Path

import numpy as np
import pytest

from mcrawsome.camera.shading import shading_gains
from mcrawsome.develop.crop import CropBox, crop_metadata, find_black_edges, parse_crop
from mcrawsome.errors import McError
from mcrawsome.io.metadata import FrameMetadata
from mcrawsome.output.dng import gain_map_opcodes, parse_gain_maps, read_dng_tags
from mcrawsome.output.dng_export import export_dng
from mcrawsome.output.export import frame_job, read_clip_facts
from mcrawsome.recipe.load import RecipeError, build_recipe

BLACK, WHITE = 64.0, 1023.0


def _scene(height: int = 64, width: int = 128) -> np.ndarray:
    rng = np.random.default_rng(0)
    return rng.integers(300, 900, (height, width)).astype(np.uint16)


def test_a_black_strip_on_the_right_is_found() -> None:
    frame = _scene()
    frame[:, -6:] = rng_black(frame[:, -6:].shape)
    assert find_black_edges([frame, frame], BLACK, WHITE) == CropBox(0, 0, 122, 64)


def test_a_strip_of_odd_width_keeps_the_bayer_phase() -> None:
    frame = _scene()
    frame[:5, :] = rng_black((5, 128))  # five black rows at the top
    assert find_black_edges([frame], BLACK, WHITE) == CropBox(0, 6, 128, 58)


def test_a_clean_frame_or_a_dark_scene_is_not_cropped() -> None:
    assert find_black_edges([_scene()], BLACK, WHITE) is None
    dark = rng_black((64, 128))
    assert find_black_edges([dark], BLACK, WHITE) is None


def test_a_strip_must_be_in_every_frame_checked() -> None:
    striped, clean = _scene(), _scene()
    striped[:, :4] = rng_black((64, 4))
    assert find_black_edges([striped, clean], BLACK, WHITE) is None


def rng_black(shape: tuple[int, ...]) -> np.ndarray:
    return (BLACK + np.random.default_rng(1).normal(0, 2, shape)).clip(0).astype(np.uint16)


def test_crop_settings_parse() -> None:
    assert parse_crop("none") == "none"
    assert parse_crop("auto") == "auto"
    assert parse_crop("1920x1080") == CropBox(0, 0, 1920, 1080)
    assert parse_crop("1920x1080+64+32") == CropBox(64, 32, 1920, 1080)
    assert str(parse_crop("1920x1080+64+32")) == "1920x1080+64+32"
    for bad in ("1921x1080", "big", "0x10", "1920x1080+63+32", "1920x1080+64"):
        with pytest.raises(ValueError, match="crop"):
            parse_crop(bad)
    with pytest.raises(RecipeError) as caught:
        build_recipe([{"develop": {"crop": "12x7"}}])
    assert caught.value.field == "develop.crop"


def test_crop_and_forced_stabilisation_are_refused() -> None:
    with pytest.raises(RecipeError, match="crop"):
        build_recipe([{"develop": {"crop": "auto"}, "motion": {"stabilise": "on"}}])


def _frame() -> FrameMetadata:
    data = json.loads((Path(__file__).parent.parent / "data" / "frame_metadata_174122.json").read_text())
    return FrameMetadata.model_validate(data)


def test_a_cropped_frame_keeps_its_part_of_the_shading_map() -> None:
    frame = _frame()
    box = CropBox(64, 32, frame.width - 128, frame.height - 96)
    cropped = crop_metadata(frame, box)
    assert (cropped.width, cropped.height) == (box.width, box.height)
    for whole, plane in zip(shading_gains(frame), shading_gains(cropped), strict=True):
        inside = whole[16 : 16 + box.height // 2, 32 : 32 + box.width // 2]
        np.testing.assert_allclose(plane, inside, rtol=1e-5)
    full = parse_gain_maps(gain_map_opcodes(frame, "rggb"))[0]
    part = parse_gain_maps(gain_map_opcodes(cropped, "rggb"))[0]
    ys = (np.arange(0, box.height, 2) + 0.5) / box.height
    xs = (np.arange(0, box.width, 2) + 0.5) / box.width
    full_ys = (np.arange(32, 32 + box.height, 2) + 0.5) / frame.height
    full_xs = (np.arange(64, 64 + box.width, 2) + 0.5) / frame.width
    np.testing.assert_allclose(part.evaluate(ys, xs), full.evaluate(full_ys, full_xs), rtol=1e-6)


def test_a_custom_crop_in_a_dng_export(tmp_path: Path, make_mcraw: Callable[..., Path]) -> None:
    clip = make_mcraw(tmp_path / "clip.mcraw", [0, 41_666_667])
    recipe = build_recipe([{"output": {"kind": "dng"}, "develop": {"crop": "64x32"}}])
    result = export_dng(clip, tmp_path / "clip_DNG", recipe, workers=1)
    tags = read_dng_tags(min(result.output.glob("*.dng")))
    assert (tags[256], tags[257]) == ((64,), (32,))
    assert "crop = " in result.sidecar.read_text()


def test_frame_jobs_and_facts_carry_the_crop(tmp_path: Path, make_mcraw: Callable[..., Path]) -> None:
    clip = make_mcraw(tmp_path / "clip.mcraw", [0, 41_666_667])
    recipe = build_recipe([{"develop": {"crop": "64x32"}}])
    facts = read_clip_facts(clip, recipe)
    assert (facts.width, facts.height) == (64, 32)
    assert frame_job(clip, recipe, facts).crop == (0, 0, 64, 32)


def test_a_crop_larger_than_the_frame_is_refused(tmp_path: Path, make_mcraw: Callable[..., Path]) -> None:
    clip = make_mcraw(tmp_path / "clip.mcraw", [0, 41_666_667])
    recipe = build_recipe([{"output": {"kind": "dng"}, "develop": {"crop": "256x32"}}])
    with pytest.raises(McError, match="does not fit the frame") as caught:
        export_dng(clip, tmp_path / "clip_DNG", recipe, workers=1)
    assert caught.value.code == "recipe-invalid"


def test_rendered_frames_are_cropped(tmp_path: Path, make_mcraw: Callable[..., Path]) -> None:
    from mcrawsome.pipeline.render import iter_frames

    clip = make_mcraw(tmp_path / "clip.mcraw", [0, 41_666_667, 83_333_334])
    recipe = build_recipe([{"output": {"kind": "exr"}, "develop": {"crop": "64x32"}}])
    job = frame_job(clip, recipe, read_clip_facts(clip, recipe))
    frames = list(iter_frames(job, 3, 1))
    assert [f.shape for f in frames] == [(32, 64, 3)] * 3
