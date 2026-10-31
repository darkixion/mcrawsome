from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from mcrawsome.develop.raw import Developer
from mcrawsome.develop.white_balance import pick_white_balance
from mcrawsome.io.clip import Clip, RawFrame
from mcrawsome.output.export import read_clip_facts
from mcrawsome.recipe.load import build_recipe


def test_picked_rectangle_develops_with_equal_camera_channel_means(white_balance_raw: Path) -> None:
    recipe = build_recipe([{"develop": {"lens_shading": 0}}])
    kelvin, tint = pick_white_balance(white_balance_raw, recipe, 1, (16, 16, 24, 24))
    custom = build_recipe(
        [
            {
                "develop": {
                    "lens_shading": 0,
                    "white_balance": "custom",
                    "temperature": kelvin,
                    "tint": tint,
                }
            }
        ]
    )
    facts = read_clip_facts(white_balance_raw, custom, audio=False)
    with Clip(white_balance_raw) as clip:
        developer = Developer(clip.metadata, clip.frame_metadata(0), facts.neutral, lens_shading=0)
        rgb = developer.camera_rgb(clip.raw_frame(1).data)[16:40, 16:40]
    means = rgb.mean(axis=(0, 1))
    assert means / means[1] == pytest.approx(np.ones(3), abs=0.002)
    assert read_clip_facts(white_balance_raw, recipe, audio=False).neutral != facts.neutral


@pytest.mark.parametrize(
    ("index", "rectangle"),
    [(-1, (0, 0, 8, 8)), (3, (0, 0, 8, 8)), (0, (-1, 0, 8, 8)), (0, (0, 0, 0, 4)), (0, (120, 0, 16, 8))],
)
def test_picker_refuses_invalid_coordinates(
    white_balance_raw: Path, index: int, rectangle: tuple[int, int, int, int]
) -> None:
    with pytest.raises(ValueError, match=r"frame|rectangle"):
        pick_white_balance(white_balance_raw, build_recipe([]), index, rectangle)


@pytest.mark.parametrize("value", [64, 1023])
def test_picker_refuses_black_or_clipped_patches(
    white_balance_raw: Path, monkeypatch: pytest.MonkeyPatch, value: int
) -> None:
    original = Clip.raw_frame

    def frame(clip: Clip, index: int) -> RawFrame:
        source = original(clip, index)
        return replace(source, data=np.full_like(source.data, value))

    monkeypatch.setattr(Clip, "raw_frame", frame)
    with pytest.raises(ValueError, match="mostly clipped or black"):
        pick_white_balance(white_balance_raw, build_recipe([]), 0, (16, 16, 24, 24))


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
@pytest.mark.parametrize("recorded", [0, 3])
def test_picker_maps_crop_and_orientation_to_the_preview(
    white_balance_raw: Path, monkeypatch: pytest.MonkeyPatch, rotation: int, recorded: int
) -> None:
    original, original_metadata = Clip.raw_frame, Clip.frame_metadata

    def frame(clip: Clip, index: int) -> RawFrame:
        source = original(clip, index)
        raw = np.full_like(source.data, 64)
        raw[8:32, 16:48] = source.data[8:32, 16:48]
        return replace(source, data=raw)

    from mcrawsome.io.metadata import FrameMetadata

    def metadata(clip: Clip, index: int) -> FrameMetadata:
        return original_metadata(clip, index).model_copy(update={"orientation": recorded})

    monkeypatch.setattr(Clip, "raw_frame", frame)
    monkeypatch.setattr(Clip, "frame_metadata", metadata)
    mask = np.zeros((48, 64), bool)
    mask[12:28, 20:44] = True
    shown = np.rot90(mask, -rotation // 90 + (2 if recorded == 3 else 0))
    ys, xs = np.where(shown)
    rectangle = int(xs.min()), int(ys.min()), int(xs.max() - xs.min() + 1), int(ys.max() - ys.min() + 1)
    recipe = build_recipe([{"develop": {"crop": "64x48", "rotation": rotation, "lens_shading": 0}}])
    kelvin, tint = pick_white_balance(white_balance_raw, recipe, 1, rectangle)
    assert kelvin == pytest.approx(5000, abs=100)
    assert tint == pytest.approx(0, abs=5)


def test_picker_undoes_the_current_custom_balance(white_balance_raw: Path) -> None:
    base = build_recipe([{"develop": {"lens_shading": 0}}])
    custom = build_recipe(
        [
            {
                "develop": {
                    "lens_shading": 0,
                    "white_balance": "custom",
                    "temperature": 7000,
                    "tint": 20,
                }
            }
        ]
    )
    first = pick_white_balance(white_balance_raw, base, 1, (16, 16, 24, 24))
    second = pick_white_balance(white_balance_raw, custom, 1, (16, 16, 24, 24))
    assert second[0] == pytest.approx(first[0], abs=1)
    assert second[1] == pytest.approx(first[1], abs=0.1)


def test_picker_does_not_measure_reconstructed_highlights(
    white_balance_raw: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recipe = build_recipe([{"develop": {"lens_shading": 0}}])
    expected = pick_white_balance(white_balance_raw, recipe, 1, (20, 16, 24, 24))
    original = Clip.raw_frame

    def frame(clip: Clip, index: int) -> RawFrame:
        source = original(clip, index)
        raw = source.data.copy()
        raw[:, :16] = 1023
        return replace(source, data=raw)

    monkeypatch.setattr(Clip, "raw_frame", frame)
    actual = pick_white_balance(white_balance_raw, recipe, 1, (20, 16, 24, 24))
    assert actual[0] == pytest.approx(expected[0], abs=1)
    assert actual[1] == pytest.approx(expected[1], abs=0.1)
