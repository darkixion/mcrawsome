"""Log-encoded DNGs: the curve, its linearisation table, dithering and the stored bit depth."""

from collections.abc import Callable
from pathlib import Path

import numpy as np
import pytest
import rawpy

from mcrawsome.errors import McError
from mcrawsome.output import dng_log
from mcrawsome.output.chunks import WorkFolder
from mcrawsome.output.dng import read_dng_tags
from mcrawsome.output.dng_export import export_dng
from mcrawsome.recipe.load import build_recipe


def test_the_table_rises_from_zero_to_the_white_level() -> None:
    table = np.array(dng_log.table(1023, 8))
    assert len(table) == 256
    assert table[0] == 0
    assert table[-1] == 1023
    assert np.all(np.diff(table) >= 0)


@pytest.mark.parametrize(("white", "out_bits"), [(1023, 8), (4095, 8), (16380, 10), (1023, 10)])
def test_each_code_decodes_and_encodes_back_within_one_code(white: int, out_bits: int) -> None:
    table = np.array(dng_log.table(white, out_bits), np.uint16)
    codes = dng_log.encode(table, white, out_bits, seed=None)
    assert np.abs(codes.astype(int) - np.arange(len(table))).max() <= 1
    assert len(set(table.tolist())) >= len(table) - 2  # no codes wasted on steps finer than the linear ones


def test_the_darkest_code_step_is_one_linear_step() -> None:
    table = dng_log.table(4095, 8)
    assert table[1] - table[0] == 1
    assert table[-1] - table[-2] > 16  # the highlights get the coarse steps


def test_dithering_keeps_the_mean_of_a_flat_area() -> None:
    for level in (40, 300, 900):
        flat = np.full((64, 64), level, np.uint16)
        codes = dng_log.encode(flat, 1023, 6, seed=3)
        assert len(np.unique(codes)) > 1  # dithered between neighbouring codes
        decoded = np.array(dng_log.table(1023, 6))[codes]
        assert decoded.mean() == pytest.approx(level, rel=0.005)


def test_the_same_seed_gives_the_same_codes() -> None:
    values = np.random.default_rng(0).integers(0, 1024, (16, 16), dtype=np.uint16)
    np.testing.assert_array_equal(
        dng_log.encode(values, 1023, 8, seed=5), dng_log.encode(values, 1023, 8, seed=5)
    )
    assert not np.array_equal(
        dng_log.encode(values, 1023, 8, seed=5), dng_log.encode(values, 1023, 8, seed=6)
    )


def test_output_bits() -> None:
    assert dng_log.output_bits(10, 0, "auto") == 8
    assert dng_log.output_bits(10, 2, "auto") == 8  # auto absorbs a baked vignette's overhead
    assert dng_log.output_bits(12, 0, 4) == 8
    assert dng_log.output_bits(12, 2, 2) == 12


@pytest.mark.parametrize("compression", ["none", "lossless"])
def test_a_log_dng_export(compression: str, tmp_path: Path, make_mcraw: Callable[..., Path]) -> None:
    clip = make_mcraw(tmp_path / "clip.mcraw", [0, 41_666_667, 83_333_334])
    linear = export_dng(clip, tmp_path / "linear_DNG", build_recipe([{"output": {"kind": "dng"}}]), workers=1)
    recipe = build_recipe(
        [{"output": {"kind": "dng", "dng_transfer": "log", "dng_compression": compression}}]
    )
    result = export_dng(clip, tmp_path / "clip_DNG", recipe, workers=1)
    path = min(result.output.glob("*.dng"))
    tags = read_dng_tags(path)
    white = read_dng_tags(min(linear.output.glob("*.dng")))[50717][0]
    bits = dng_log.output_bits(white.bit_length(), 0, "auto")
    assert len(tags[50712]) == 2**bits
    assert tags[50717] == (white,)
    assert tags[258] == (bits if compression == "lossless" else 8,)
    if compression == "none":  # one byte per value instead of two
        assert tags[279][0] * 2 == read_dng_tags(min(linear.output.glob("*.dng")))[279][0]
    with rawpy.imread(str(path)) as raw, rawpy.imread(str(min(linear.output.glob("*.dng")))) as ref:
        # LibRaw applies the table: the linearised values are close to the linear DNG's
        error = np.abs(raw.raw_image.astype(np.float64) - ref.raw_image.astype(np.float64))
        assert error.mean() < 0.02 * white


def test_a_resumed_log_export_writes_the_same_bytes(tmp_path: Path, make_mcraw: Callable[..., Path]) -> None:
    clip = make_mcraw(tmp_path / "clip.mcraw", [0, 41_666_667, 83_333_334])
    recipe = build_recipe([{"output": {"kind": "dng", "dng_transfer": "log"}}])
    folder = tmp_path / "clip_DNG"
    with pytest.raises(McError):
        export_dng(clip, folder, recipe, workers=1, stop=lambda: True)
    assert WorkFolder(folder).publish_path.exists()
    export_dng(clip, folder, recipe, workers=1)
    whole = export_dng(clip, tmp_path / "whole_DNG", recipe, workers=1)
    for path in whole.output.glob("*.dng"):
        assert (folder / path.name).read_bytes() == path.read_bytes()


def test_too_few_bits_are_refused(tmp_path: Path, make_mcraw: Callable[..., Path]) -> None:
    clip = make_mcraw(tmp_path / "clip.mcraw", [0, 41_666_667])
    recipe = build_recipe([{"output": {"kind": "dng", "dng_transfer": "log", "dng_reduce_bits": "8"}}])
    with pytest.raises(McError, match="at least 6") as caught:
        export_dng(clip, tmp_path / "clip_DNG", recipe, workers=1)
    assert caught.value.code == "recipe-invalid"


def test_linear_dngs_are_the_default() -> None:
    output = build_recipe([{}]).output
    assert (output.dng_transfer, output.dng_reduce_bits) == ("linear", "auto")
