import json
from collections.abc import Callable
from pathlib import Path

import numpy as np
import pytest

from mcrawsome.develop.raw import Developer, levels
from mcrawsome.errors import McError
from mcrawsome.io.metadata import FrameMetadata, parse_container_metadata
from mcrawsome.output.dng import read_dng_tags
from mcrawsome.output.dng_export import export_dng
from mcrawsome.output.export import frame_job, read_clip_facts
from mcrawsome.recipe.load import build_recipe

DATA = Path(__file__).parent.parent / "data"
CONTAINER = parse_container_metadata(json.loads((DATA / "container_metadata_pixel10.json").read_text()))
NEUTRAL = (0.5, 1.0, 0.6)


def _frame() -> FrameMetadata:
    return FrameMetadata.model_validate(
        {
            "width": 32, "height": 16, "originalWidth": 32, "originalHeight": 16, "orientation": 0,
            "compressionType": 7, "asShotNeutral": list(NEUTRAL), "dynamicBlackLevel": [256, 256, 260, 256],
            "dynamicWhiteLevel": 4095, "iso": 50, "exposureTime": 1e7, "lensShadingMap": [[1.0] * 4] * 4,
            "lensShadingMapWidth": 2, "lensShadingMapHeight": 2, "recvdTimestampMs": 0,
        }
    )  # fmt: skip


def test_levels_without_overrides_are_the_frames() -> None:
    assert levels(_frame(), None, None) == ((256.0, 256.0, 260.0, 256.0), 4095.0)


def test_levels_take_one_or_four_black_values_and_a_white_value() -> None:
    assert levels(_frame(), 64.0, None) == ((64.0, 64.0, 64.0, 64.0), 4095.0)
    assert levels(_frame(), (60.0, 61.0, 62.0, 63.0), 1000.0) == ((60.0, 61.0, 62.0, 63.0), 1000.0)


def test_a_lower_black_override_brightens_the_picture() -> None:
    raw = np.full((16, 32), 1000, np.uint16)
    plain = Developer(CONTAINER, _frame(), NEUTRAL).develop(raw)
    lower = Developer(CONTAINER, _frame(), NEUTRAL, black_level=0.0).develop(raw)
    assert float(lower.mean()) > float(plain.mean()) * 1.2


def test_without_overrides_the_development_is_unchanged() -> None:
    raw = np.random.default_rng(1).integers(256, 4095, (16, 32), dtype=np.uint16)
    plain = Developer(CONTAINER, _frame(), NEUTRAL).develop(raw)
    same = Developer(CONTAINER, _frame(), NEUTRAL, black_level=None, white_level=None).develop(raw)
    np.testing.assert_array_equal(plain, same)


def test_a_white_override_moves_the_clipping_point() -> None:
    developer = Developer(CONTAINER, _frame(), NEUTRAL, white_level=800.0, highlight_recovery=0.0)
    assert developer.white == 800.0
    raw = np.full((16, 32), 900, np.uint16)
    out = developer.develop(raw)  # every photosite is over white: the picture is neutral there
    centre = out[4:-4, 4:-4].reshape(-1, 3).mean(axis=0)
    assert np.ptp(centre) < 0.02 * centre.max()


def test_frame_jobs_carry_the_overrides(tmp_path: Path, make_mcraw: Callable[..., Path]) -> None:
    clip = make_mcraw(tmp_path / "clip.mcraw", [0, 41_666_667])
    recipe = build_recipe([{"develop": {"black_level": [60, 61, 62, 63], "white_level": 1000}}])
    job = frame_job(clip, recipe, read_clip_facts(clip, recipe))
    assert job.black_level == (60.0, 61.0, 62.0, 63.0)
    assert job.white_level == 1000.0


def test_dngs_carry_the_overrides(tmp_path: Path, make_mcraw: Callable[..., Path]) -> None:
    clip = make_mcraw(tmp_path / "clip.mcraw", [0, 41_666_667])
    recipe = build_recipe([{"output": {"kind": "dng"}, "develop": {"black_level": 32, "white_level": 900}}])
    result = export_dng(clip, tmp_path / "clip_DNG", recipe, workers=1)
    tags = read_dng_tags(min(result.output.glob("*.dng")))
    assert tags[50714] == pytest.approx((32.0, 32.0, 32.0, 32.0))
    assert tags[50717] == (900,)


def test_a_white_level_at_or_below_black_is_refused(tmp_path: Path, make_mcraw: Callable[..., Path]) -> None:
    clip = make_mcraw(tmp_path / "clip.mcraw", [0, 41_666_667])
    recipe = build_recipe([{"output": {"kind": "dng"}, "develop": {"white_level": 40}}])
    with pytest.raises(McError, match=r"develop.white_level \(40\) must be above the black level") as caught:
        export_dng(clip, tmp_path / "clip_DNG", recipe, workers=1)
    assert caught.value.code == "recipe-invalid"


def test_frame_jobs_carry_the_lens_shading_mode(tmp_path: Path, make_mcraw: Callable[..., Path]) -> None:
    clip = make_mcraw(tmp_path / "clip.mcraw", [0, 41_666_667])
    recipe = build_recipe([{"develop": {"lens_shading_mode": "colour"}}])
    assert frame_job(clip, recipe, read_clip_facts(clip, recipe)).lens_shading_mode == "colour"
