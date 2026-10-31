"""The lens shading baked into DNG raw values (output.dng_shading = "baked"), with and without Scale Raw."""

import json
from collections.abc import Callable
from fractions import Fraction
from pathlib import Path

import numpy as np
import numpy.typing as npt
import pytest
import rawpy

from mcrawsome.io.metadata import FrameMetadata, parse_container_metadata
from mcrawsome.output.dng import bake_shading, dng_info, read_dng_tags, shading_scale_bits, write_dng
from mcrawsome.output.dng_export import export_dng
from mcrawsome.recipe.load import build_recipe

DATA = Path(__file__).parent.parent / "data"
BLACK, WHITE = 64.0, 1023.0


def _mosaic(path: Path) -> npt.NDArray[np.uint16]:
    with rawpy.imread(str(path)) as image:
        return image.raw_image.copy()


def _frame(gain: float, colour_gain: float = 1.0) -> FrameMetadata:
    planes = [[gain * colour_gain] * 4, [gain] * 4, [gain] * 4, [gain] * 4]
    return FrameMetadata.model_validate(
        {
            "width": 16, "height": 8, "originalWidth": 16, "originalHeight": 8, "orientation": 0,
            "compressionType": 7, "asShotNeutral": [0.5, 1.0, 0.7], "dynamicBlackLevel": [BLACK] * 4,
            "dynamicWhiteLevel": WHITE, "iso": 100.0, "exposureTime": 1e7, "lensShadingMap": planes,
            "lensShadingMapWidth": 2, "lensShadingMapHeight": 2, "recvdTimestampMs": 0,
        }
    )  # fmt: skip


def test_scale_bits_round_the_largest_gain_up_to_a_power_of_two() -> None:
    assert shading_scale_bits(_frame(1.0), 1.0, "full") == 0
    assert shading_scale_bits(_frame(3.0), 1.0, "full") == 2
    assert shading_scale_bits(_frame(4.0), 1.0, "full") == 2
    assert shading_scale_bits(_frame(4.0), 0.5, "full") == 1  # strength 0.5: a gain of 2
    assert shading_scale_bits(_frame(4.0, 1.5), 1.0, "colour") == 0  # colour gains are never above 1


def test_baking_with_scale_raises_the_white_level_and_clips_nothing() -> None:
    raw = np.full((8, 16), BLACK + 200, np.uint16)
    raw[0, 0] = WHITE  # an exposed-to-the-right highlight
    baked, white = bake_shading(raw, _frame(4.0), "rggb", 1.0, "full", (BLACK,) * 4, WHITE, 2)
    assert white == round(BLACK + (WHITE - BLACK) * 4)
    assert baked[1, 1] == BLACK + 800
    assert baked[0, 0] == white  # the highlight is kept, at the new white level


def test_baking_without_scale_clips_at_the_sensor_white() -> None:
    raw = np.full((8, 16), BLACK + (WHITE - BLACK) / 2, np.uint16)
    baked, white = bake_shading(raw, _frame(4.0), "rggb", 1.0, "full", (BLACK,) * 4, WHITE, 0)
    assert white == WHITE
    assert int(baked.max()) == WHITE


def test_a_gain_of_one_leaves_the_values() -> None:
    raw = np.random.default_rng(1).integers(0, 1024, (8, 16), dtype=np.uint16)
    baked, white = bake_shading(raw, _frame(1.0), "rggb", 1.0, "full", (BLACK,) * 4, WHITE, 0)
    np.testing.assert_array_equal(baked, raw)
    assert white == WHITE


def test_a_baked_dng_has_no_gain_maps_and_a_higher_baseline_exposure(tmp_path: Path) -> None:
    container = parse_container_metadata(json.loads((DATA / "container_metadata_pixel10.json").read_text()))
    info = dng_info(container, _frame(4.0), Fraction(24), 0.5, baked_bits=2)
    path = tmp_path / "f.dng"
    write_dng(path, np.full((8, 16), 100, np.uint16), info)
    tags = read_dng_tags(path)
    assert 51009 not in tags
    assert tags[50730] == pytest.approx((2.5,))


def test_baked_dng_export(tmp_path: Path, make_mcraw: Callable[..., Path]) -> None:
    clip = make_mcraw(tmp_path / "clip.mcraw", [0, 41_666_667])
    plain = export_dng(clip, tmp_path / "plain_DNG", build_recipe([{"output": {"kind": "dng"}}]), workers=1)
    recipe = build_recipe([{"output": {"kind": "dng", "dng_shading": "baked"}}])
    result = export_dng(clip, tmp_path / "clip_DNG", recipe, workers=1)
    tags = read_dng_tags(min(result.output.glob("*.dng")))
    before = read_dng_tags(min(plain.output.glob("*.dng")))
    assert 51009 not in tags
    assert 51009 in before
    text = result.sidecar.read_text()
    assert "dng_shading_scale_bits" in text
    bits = int(text.split("dng_shading_scale_bits = ")[1].split()[0])
    assert tags[50730][0] == pytest.approx(before[50730][0] + bits)
    assert tags[50717][0] >= before[50717][0]


def test_dng_settings_are_noted_as_ignored_for_videos() -> None:
    from mcrawsome.output.export import ignored_settings

    recipe = build_recipe([{"output": {"kind": "exr", "dng_shading": "baked"}}])
    assert "output.dng_shading: CinemaDNG only; no effect here" in ignored_settings(recipe)
    assert not any("CinemaDNG only" in n for n in ignored_settings(build_recipe([{}])))


def test_scale_uses_the_largest_gain_in_the_whole_clip(
    tmp_path: Path, make_mcraw: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    from mcrawsome.io.clip import Clip
    from mcrawsome.output.dng_export import _baked_bits

    path = make_mcraw(tmp_path / "clip.mcraw", [0, 41_666_667])
    monkeypatch.setattr(Clip, "frame_metadata", lambda _self, i: _frame(1.0 if i == 0 else 4.0))
    recipe = build_recipe([{"output": {"kind": "dng", "dng_shading": "baked"}}])
    assert _baked_bits(path, recipe) == 2


def test_scale_refuses_a_range_that_cannot_fit_in_16_bits() -> None:
    with pytest.raises(ValueError, match="16-bit"):
        bake_shading(
            np.full((8, 16), 20000, np.uint16),
            _frame(4.0),
            "rggb",
            1.0,
            "full",
            (BLACK,) * 4,
            20000,
            2,
        )


def test_profile_override_sets_baked_headroom_and_pixels(
    tmp_path: Path, make_mcraw: Callable[..., Path]
) -> None:
    from mcrawsome.camera.vignette import capture_profile
    from mcrawsome.io.clip import Clip
    from mcrawsome.output.dng import parse_gain_maps
    from mcrawsome.output.dng_export import _baked_bits

    path = make_mcraw(tmp_path / "clip.mcraw", [0, 41_666_667])
    with Clip(path) as clip:
        profile = capture_profile(clip.metadata, clip.frame_metadata(0)).model_dump(mode="json")
        original = clip.raw_frame(0)
    profile["gains"] = [[4.0] * (profile["map_width"] * profile["map_height"])] * 4
    values = {"develop": {"vignette_profile": profile}, "output": {"kind": "dng"}}
    mapped = export_dng(path, tmp_path / "mapped", build_recipe([values]), workers=1)
    first = min(mapped.output.glob("*.dng"))
    maps = parse_gain_maps(bytes(read_dng_tags(first)[51009]))
    assert all(float(g.gains.min()) == 4.0 for g in maps)
    np.testing.assert_array_equal(_mosaic(first), original.data)
    recipe = build_recipe([values, {"output": {"dng_shading": "baked"}}])
    assert _baked_bits(path, recipe) == 2
    baked = export_dng(path, tmp_path / "baked", recipe, workers=1)
    first = min(baked.output.glob("*.dng"))
    assert 51009 not in read_dng_tags(first)
    black = original.metadata.black_level[0]
    np.testing.assert_array_equal(_mosaic(first), black + (original.data - black) * 4)


def test_an_unstorable_baked_export_is_a_recipe_error(
    tmp_path: Path, make_mcraw: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    from mcrawsome.errors import McError
    from mcrawsome.io.clip import Clip
    from mcrawsome.output.dng_export import _baked_bits

    path = make_mcraw(tmp_path / "clip.mcraw", [0, 41_666_667])
    monkeypatch.setattr(Clip, "frame_metadata", lambda _self, _i: _frame(4.0))
    recipe = build_recipe(
        [{"output": {"kind": "dng", "dng_shading": "baked"}, "develop": {"white_level": 20000}}]
    )
    with pytest.raises(McError, match="16-bit") as caught:
        _baked_bits(path, recipe)
    assert caught.value.code == "recipe-invalid"


def _varying(scale: float = 1.0) -> FrameMetadata:
    """A 2x2 shading map whose gains differ by site and position, as a real lens's do."""
    planes = [
        [g * scale for g in (3.2, 1.4, 2.1, 1.0)],
        [g * scale for g in (2.6, 1.2, 1.9, 1.1)],
        [g * scale for g in (2.5, 1.3, 1.8, 1.0)],
        [g * scale for g in (3.7, 1.5, 2.4, 1.2)],
    ]
    return _frame(1.0).model_copy(update={"lens_shading_map": [v for plane in planes for v in plane]})


@pytest.mark.parametrize("pattern", ["rggb", "grbg", "bggr"])
@pytest.mark.parametrize(
    ("strength", "mode", "bits"), [(1.0, "full", 2), (0.5, "full", 2), (1.0, "colour", 0)]
)
def test_the_mount_baker_gives_the_exports_values(
    pattern: str, strength: float, mode: str, bits: int
) -> None:
    from mcrawsome.output.dng import ShadingBaker, baked_white

    raw = np.random.default_rng(3).integers(0, 1024, (8, 16), dtype=np.uint16)
    blacks = (60.0, 64.0, 62.0, 66.0)
    expected, white = bake_shading(raw, _varying(), pattern, strength, mode, blacks, WHITE, bits)  # type: ignore[arg-type]
    baker = ShadingBaker(pattern, strength, mode, bits)  # type: ignore[arg-type]
    np.testing.assert_array_equal(baker.bake(raw, _varying(), blacks, WHITE), expected)
    assert baked_white(blacks, WHITE, bits) == white


def test_the_mount_baker_reuses_its_gains_until_the_map_changes(monkeypatch: pytest.MonkeyPatch) -> None:
    from mcrawsome.camera.shading import shading_gains as original
    from mcrawsome.output import dng

    made: list[int] = []

    def counted(frame: FrameMetadata, mode: str) -> object:
        made.append(1)
        return original(frame, mode)  # type: ignore[arg-type]

    monkeypatch.setattr("mcrawsome.output.dng.shading_gains", counted)
    baker = dng.ShadingBaker("rggb", 1.0, "full", 2)
    raw = np.full((8, 16), 300, np.uint16)
    for _ in range(3):
        baker.bake(raw, _varying(), (BLACK,) * 4, WHITE)
    assert len(made) == 1
    baker.bake(raw, _varying(1.1), (BLACK,) * 4, WHITE)
    assert len(made) == 2


def test_mount_headroom_is_fixed_and_needs_only_the_first_frame() -> None:
    from mcrawsome.output.dng import mount_bake_bits

    blacks = (BLACK,) * 4
    assert mount_bake_bits(_frame(1.0), 1.0, "full", blacks, WHITE) == 2  # always 12 to 14 bits, as Tools
    assert mount_bake_bits(_frame(3.7), 1.0, "full", blacks, WHITE) == 2
    assert mount_bake_bits(_frame(5.0), 1.0, "full", blacks, WHITE) == 3  # this first frame needs more
    assert mount_bake_bits(_frame(4.0, 1.5), 1.0, "colour", blacks, WHITE) == 0  # no gain above 1
    assert mount_bake_bits(_frame(5.0), 1.0, "full", (64.0,) * 4, 16383) == 2  # 3 bits would pass 65535
    assert mount_bake_bits(_frame(3.0), 1.0, "full", (64.0,) * 4, 65535) == 0


def test_the_baker_can_scale_exposure_without_the_shading_map() -> None:
    from mcrawsome.output.dng import ShadingBaker

    raw = np.full((8, 16), BLACK + 100, np.uint16)
    raw[0, 0] = WHITE
    baker = ShadingBaker("rggb", 1.0, "full", 1, shading=False)
    scaled = baker.bake(raw, _varying(), (BLACK,) * 4, WHITE, gain=2.0)
    assert int(scaled[1, 1]) == BLACK + 200  # no shading gain, only the exposure
    assert int(scaled[0, 0]) == baker.white((BLACK,) * 4, WHITE)  # clipped at the raised white level


def test_exposure_gain_brings_each_frame_to_the_first_frames_exposure() -> None:
    from mcrawsome.output.dng import exposure_gain

    first = _frame(1.0)
    darker = first.model_copy(update={"iso": first.iso / 2})
    shorter = first.model_copy(update={"exposure_time_ns": first.exposure_time_ns / 4})
    assert exposure_gain(first, first) == 1.0
    assert exposure_gain(first, darker) == 2.0
    assert exposure_gain(first, shorter) == 4.0
    assert exposure_gain(first, first.model_copy(update={"iso": 0.0})) == 1.0  # unknown exposure: unchanged
