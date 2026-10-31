"""Measured synthetic Resolve 21.1.1 Media Pool rename, with unrelated fields retained."""

import pytest

from mcrawsome.resolve.bin_codec import WireField, decode_wire, encode_wire
from mcrawsome.resolve.bin_name import patch_clip_name

# Only the small identifier-free clip-property records from the owner's paired synthetic exports.
_BEFORE = bytes.fromhex("0a180a040a02300412100000000000000006ffffffffffffffff")
_AFTER = bytes.fromhex(
    "0a290a150a13120f4c6f6767696e67204120436166c3a9300412100000000000000006ffffffffffffffff"
)


def nested(fields: list[WireField]) -> bytes:
    data = encode_wire(fields)
    for _ in range(3):
        data = encode_wire([WireField(1, 2, data), WireField(900, 0, 7)])
    return data


def test_matches_resolves_actual_rename_and_can_clear_the_override() -> None:
    assert patch_clip_name(_BEFORE, "Logging A Café") == _AFTER
    assert patch_clip_name(_AFTER, "Logging A Café") == _AFTER
    assert patch_clip_name(_AFTER, "") == _BEFORE
    assert patch_clip_name(_BEFORE, "  ") == _BEFORE


def test_unicode_uuid_text_and_unknown_properties_survive() -> None:
    unknown = WireField(800, 5, b"abcd")
    title = "東京 00000000-0000-4000-8000-000000000001"
    before = nested([WireField(6, 0, 4), unknown])
    after = patch_clip_name(before, title)
    expected = nested([WireField(2, 2, title.encode()), WireField(6, 0, 4), unknown])
    assert after == expected
    assert patch_clip_name(after, " New title ") == nested(
        [WireField(2, 2, b" New title "), WireField(6, 0, 4), unknown]
    )


@pytest.mark.parametrize(
    "data",
    [
        b"",
        _BEFORE[:-1],
        _BEFORE + _BEFORE,
        nested([WireField(6, 0, 3)]),
        nested([WireField(6, 0, 4), WireField(2, 0, 1)]),
        nested([WireField(6, 0, 4), WireField(2, 2, b"a"), WireField(2, 2, b"b")]),
    ],
)
def test_unfamiliar_or_duplicate_name_targets_are_refused(data: bytes) -> None:
    with pytest.raises(ValueError, match="Resolve"):
        patch_clip_name(data, "Title")


def test_unknown_parent_fields_remain_in_place() -> None:
    before = decode_wire(_BEFORE)
    extra = WireField(42, 2, b"unrelated")
    source = encode_wire([extra, *before, extra])
    assert patch_clip_name(source, "Logging A Café") == encode_wire([extra, *decode_wire(_AFTER), extra])
