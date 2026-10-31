"""Clip rotation matches a native Resolve portrait export without changing RAW defaults."""

import json
from pathlib import Path

import pytest

from mcrawsome.resolve.bin_codec import WireField, decode_wire, encode_wire
from mcrawsome.resolve.rotation import clip_rotation

_FIXTURE = json.loads((Path(__file__).parent / "data/clip_rotation_21_1.json").read_text())
_RECORDED = bytes.fromhex(_FIXTURE["recorded"])
_PORTRAIT = bytes.fromhex(_FIXTURE["clockwise_90"])


def test_clip_properties_match_native_portrait_export_byte_for_byte() -> None:
    assert clip_rotation(_RECORDED, 90) == _PORTRAIT
    assert clip_rotation(_PORTRAIT, 0) == _RECORDED
    assert clip_rotation(_PORTRAIT, 90) == _PORTRAIT


@pytest.mark.parametrize("turn", [0, 90, 180, 270])
def test_only_source_rotation_changes_and_unknown_raw_fields_survive(turn: int) -> None:
    source = _RECORDED + encode_wire([WireField(100, 2, b"unknown native field")])
    original = decode_wire(source)
    result = decode_wire(clip_rotation(source, turn))
    assert [f for f in result if f.number != 19] == [f for f in original if f.number != 19]
    assert next(f for f in result if f.number == 19) == WireField(19, 0, 2 * turn)


@pytest.mark.parametrize("turn", [-90, 45, 360])
def test_invalid_turn_refused(turn: int) -> None:
    with pytest.raises(ValueError, match="quarter-turn"):
        clip_rotation(_RECORDED, turn)


@pytest.mark.parametrize("change", ["missing", "duplicate", "wrong-codec", "wrong-type"])
def test_invalid_native_descriptor_is_refused(change: str) -> None:
    fields = decode_wire(_RECORDED)
    if change == "missing":
        fields = [f for f in fields if f.number != 19]
    elif change == "duplicate":
        fields.append(WireField(19, 0, 0))
    elif change == "wrong-codec":
        fields = [WireField(1, 0, 0) if f.number == 1 else f for f in fields]
    else:
        fields = [WireField(19, 2, b"wrong") if f.number == 19 else f for f in fields]
    with pytest.raises(ValueError, match="Unsupported"):
        clip_rotation(encode_wire(fields), 90)
