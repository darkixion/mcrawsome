"""Deflicker baked into physical CinemaDNG raw values, as a picture-normalised mount serves them."""

import tomllib
from pathlib import Path
from typing import Any

import numpy as np
from flicker_clips import flicker_clip

from mcrawsome.mount.sequence import Sequence
from mcrawsome.output.dng import baked_white, exposure_headroom, read_dng_tags
from mcrawsome.output.dng_export import export_dng
from mcrawsome.recipe.load import build_recipe
from mcrawsome.recipe.raw import RawCalibration
from mcrawsome.recipe.schema import Recipe


def _recipe(**output: object) -> Recipe:
    return build_recipe(
        [{"develop": {"deflicker": 1.0, "deflicker_seconds": 1.0}, "output": {"kind": "dng", **output}}]
    )


def read_sidecar(path: Path) -> dict[str, Any]:
    return tomllib.loads(path.read_text())


def _pixels(path: Path) -> np.ndarray:
    tags = read_dng_tags(path)
    width, height = tags[256][0], tags[257][0]
    return np.frombuffer(path.read_bytes()[-width * height * 2 :], "<u2")


def test_baked_deflicker_writes_the_picture_mounts_values(tmp_path: Path) -> None:
    clip = flicker_clip(tmp_path / "flicker.mcraw", [200, 340] * 12)
    baseline = export_dng(clip, tmp_path / "baseline_DNG", _recipe(), workers=1)
    baked = export_dng(clip, tmp_path / "baked_DNG", _recipe(dng_deflicker="baked"), workers=1)
    picture = RawCalibration(mount_normalise_exposure="picture", mount_normalise_seconds=1.0)
    with Sequence(clip, raw_settings=picture) as sequence:
        bits = exposure_headroom(sequence.gains or [])
        assert bits == 1
        exposures = set()
        for index in range(24):
            name = f"flicker-{index:06d}.dng"
            tags, plain = read_dng_tags(baked.output / name), read_dng_tags(baseline.output / name)
            served = b"".join(sequence.read(name, at, 4096) for at in range(0, sequence.size(name), 4096))
            np.testing.assert_array_equal(
                _pixels(baked.output / name), np.frombuffer(served[-128 * 64 * 2 :], "<u2")
            )
            assert tags[50717] == (baked_white(plain[50714], plain[50717][0], bits),)
            assert tags[51009] == plain[51009]  # the lens shading stays as gain maps
            exposures.add(tags[50730][0])
        assert len(exposures) == 1  # the gains are in the values, not in BaselineExposure
    measurements = read_sidecar(baked.sidecar)["measurements"]
    assert measurements["dng_exposure_bits"] == 1
    assert "deflicker_max_stops" in measurements


def test_baked_deflicker_and_baked_shading_share_one_headroom(tmp_path: Path) -> None:
    clip = flicker_clip(tmp_path / "flicker.mcraw", [200, 340] * 12)
    shading = export_dng(clip, tmp_path / "shading_DNG", _recipe(dng_shading="baked"), workers=1)
    both = export_dng(
        clip, tmp_path / "both_DNG", _recipe(dng_shading="baked", dng_deflicker="baked"), workers=1
    )
    shading_bits = read_sidecar(shading.sidecar)["measurements"]["dng_shading_scale_bits"]
    measured = read_sidecar(both.sidecar)["measurements"]
    assert (measured["dng_shading_scale_bits"], measured["dng_exposure_bits"]) == (shading_bits, 1)
    name = "flicker-000003.dng"
    tags = read_dng_tags(both.output / name)
    assert 51009 not in tags
    recorded = read_dng_tags(export_dng(clip, tmp_path / "plain_DNG", _recipe(), workers=1).output / name)
    assert tags[50717] == (baked_white(recorded[50714], recorded[50717][0], shading_bits + 1),)
    assert tags[50730] == read_dng_tags(both.output / "flicker-000000.dng")[50730]


def test_baked_deflicker_without_a_deflicker_strength_changes_nothing(tmp_path: Path) -> None:
    clip = flicker_clip(tmp_path / "flicker.mcraw", [200, 340] * 3)
    recipe = build_recipe([{"output": {"kind": "dng", "dng_deflicker": "baked"}}])
    plain = export_dng(clip, tmp_path / "plain_DNG", build_recipe([{"output": {"kind": "dng"}}]), workers=1)
    baked = export_dng(clip, tmp_path / "baked_DNG", recipe, workers=1)
    for path in plain.output.glob("*.dng"):
        assert (baked.output / path.name).read_bytes() == path.read_bytes()
