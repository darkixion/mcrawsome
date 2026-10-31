"""Bin RAW defaults must match native import without reading or developing any pixels."""

import json
import struct
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from mcrawsome.output.dng import DngInfo, read_dng_tags, uncompressed_header
from mcrawsome.resolve.bin_white_balance import patch_white_balance, white_balance

_FIXTURE = json.loads((Path(__file__).parent / "data/white_balance_21_1.json").read_text())
_SAMPLES = _FIXTURE["samples"]


def _info(values: dict[str, Any]) -> DngInfo:
    arguments: dict[str, Any] = {
        key: tuple(value) if isinstance(value, list) else value for key, value in values.items()
    }
    return DngInfo(
        width=8, height=6, cfa="rggb", black_levels=(0, 0, 0, 0), white_level=65535,
        forward_matrix_1=None, forward_matrix_2=None, noise_profile=None, orientation=1,
        make="Synthetic", model="Bin test", frame_rate=None, baseline_exposure=0, gain_maps=b"",
        **arguments,
    )  # fmt: skip


@pytest.mark.parametrize("sample", _SAMPLES, ids=lambda sample: sample["name"])
def test_matches_native_resolve_float32_values(sample: dict[str, Any]) -> None:
    result = white_balance(_info(sample["input"]))
    assert struct.pack("<ff", *result).hex() == "".join(sample["native_float32_le"])
    assert result == struct.unpack("<ff", struct.pack("<ff", *result))


def test_source_metadata_and_written_header_give_the_same_native_values(tmp_path: Path) -> None:
    sample = _SAMPLES[1]
    info = _info(sample["source_input"])
    # A header alone is sufficient: no RAW payload or decoder is involved.
    path = tmp_path / "header.dng"
    path.write_bytes(uncompressed_header(info))
    tags = read_dng_tags(path)
    written = replace(
        info, colour_matrix_1=tags[50721], colour_matrix_2=tags[50722],
        calibration_1=tags[50723], calibration_2=tags[50724], as_shot_neutral=tags[50728],
    )  # fmt: skip
    assert white_balance(info) == white_balance(written) == white_balance(_info(sample["input"]))


def test_reversing_illuminants_and_their_matrices_preserves_white_balance() -> None:
    info = _info(_SAMPLES[1]["input"])
    reverse = replace(
        info, colour_matrix_1=info.colour_matrix_2, colour_matrix_2=info.colour_matrix_1,
        calibration_1=info.calibration_2, calibration_2=info.calibration_1,
        illuminant_1=info.illuminant_2, illuminant_2=info.illuminant_1,
    )  # fmt: skip
    assert white_balance(reverse) == white_balance(info)


def test_missing_calibration_and_analog_balance_mean_identity() -> None:
    info = replace(_info(_SAMPLES[1]["input"]), calibration_1=None, calibration_2=None, analog_balance=None)
    identity = (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0)
    explicit = replace(info, calibration_1=identity, calibration_2=identity, analog_balance=(1.0, 1.0, 1.0))
    assert white_balance(info) == white_balance(explicit)
    assert white_balance(info) != white_balance(_info(_SAMPLES[1]["input"]))


def test_neutral_and_analog_balance_normalisation_preserve_their_ratios() -> None:
    info = replace(_info(_SAMPLES[0]["input"]), analog_balance=(2.0, 1.0, 4.0))
    r, g, b = info.as_shot_neutral
    scaled = replace(info, analog_balance=(4.0, 2.0, 8.0), as_shot_neutral=(2 * r, 2 * g, 2 * b))
    assert white_balance(info) == white_balance(scaled)


def test_equal_illuminants_use_the_first_calibration_only() -> None:
    info = replace(_info(_SAMPLES[1]["input"]), illuminant_2=17)
    other = replace(info, colour_matrix_2=info.colour_matrix_1, calibration_2=info.calibration_1)
    assert white_balance(info) == white_balance(other)


@pytest.mark.parametrize(
    "changes",
    [
        {"as_shot_neutral": (0.0, 1.0, 1.0)},
        {"as_shot_neutral": (1.0, float("nan"), 1.0)},
        {"as_shot_neutral": (1.0, float("inf"), 1.0)},
        {"as_shot_neutral": (-1.0, 1.0, 1.0)},
        {"analog_balance": (1.0, 0.0, 1.0)},
        {"analog_balance": (1.0, 1.0, float("inf"))},
        {"colour_matrix_1": (0.0,) * 9},
        {"colour_matrix_2": (float("nan"),) * 9},
        {"calibration_1": (0.0,) * 9},
        {"colour_matrix_1": (1.0,) * 8},
        {"calibration_2": (1.0,) * 12},
        {"illuminant_1": 0},
        {"illuminant_2": 255},
    ],
)
def test_unusable_colour_metadata_is_refused(changes: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="white balance"):
        white_balance(replace(_info(_SAMPLES[0]["input"]), **changes))


def test_both_cached_raw_defaults_are_updated_and_other_fields_survive() -> None:
    seed = bytes.fromhex(_FIXTURE["radiometry"]["synthetic"])
    expected = seed
    for old, new in zip(_SAMPLES[0]["native_float32_le"], _SAMPLES[1]["native_float32_le"], strict=True):
        assert seed.count(bytes.fromhex(old)) == 2
        expected = expected.replace(bytes.fromhex(old), bytes.fromhex(new))
    info = _info(_SAMPLES[1]["source_input"])
    assert patch_white_balance(seed, info) == expected
    assert patch_white_balance(expected, info) == expected
    # Independent physical and virtual native imports agree. Their unknown field 16 values differ
    # from the seed and are deliberately retained, not guessed or copied from the approved source.
    native = bytes.fromhex(_FIXTURE["radiometry"]["approved"])
    assert _FIXTURE["radiometry"]["approved"] == _FIXTURE["radiometry"]["physical"]
    assert patch_white_balance(native, info) == native


def test_unknown_fields_survive_and_non_wb_float_bytes_are_not_replaced() -> None:
    seed = bytes.fromhex(_FIXTURE["radiometry"]["synthetic"])
    # Unknown fixed32 field 30 contains the seed temperature too, but is unrelated to WB.
    unrelated = bytes.fromhex("f501" + _SAMPLES[0]["native_float32_le"][0])
    info = _info(_SAMPLES[1]["input"])
    assert patch_white_balance(seed + unrelated, info) == patch_white_balance(seed, info) + unrelated


def test_wrong_codec_duplicate_targets_and_wrong_float_types_are_refused() -> None:
    seed = bytes.fromhex(_FIXTURE["radiometry"]["synthetic"])
    for data in (
        seed.replace(bytes.fromhex("08b802"), bytes.fromhex("08b902")),
        seed + seed,
        seed.replace(bytes.fromhex("5d3970aa45"), bytes.fromhex("653970aa45"), 1),
        seed.replace(bytes.fromhex("5d3970aa45"), bytes.fromhex("593970aa45"), 1),
        seed + b"\x00",
        seed + b"\xff" * 11,
        seed + b"\xff" * 65536,
    ):
        with pytest.raises(ValueError, match="radiometry"):
            patch_white_balance(data, _info(_SAMPLES[1]["input"]))


@given(st.integers(min_value=0, max_value=len(bytes.fromhex(_FIXTURE["radiometry"]["synthetic"])) - 7))
def test_incomplete_nested_radiometry_is_refused(end: int) -> None:
    seed = bytes.fromhex(_FIXTURE["radiometry"]["synthetic"])
    with pytest.raises(ValueError, match="radiometry"):
        patch_white_balance(seed[:end], _info(_SAMPLES[1]["input"]))


@given(st.binary(max_size=256))
def test_unrecognised_radiometry_fails_with_bounded_reads(data: bytes) -> None:
    with pytest.raises(ValueError, match="radiometry"):
        patch_white_balance(data, _info(_SAMPLES[1]["input"]))
