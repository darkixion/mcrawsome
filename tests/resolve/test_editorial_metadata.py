"""Synthetic editorial records using the measured Resolve 21.1 metadata grammar."""

import struct

import pytest

from mcrawsome.library.annotations import ClipAnnotations
from mcrawsome.resolve.bin_codec import (
    MapField,
    WireField,
    decode_map,
    decode_wire,
    encode_map,
    encode_wire,
    unwrap,
    wrap,
)
from mcrawsome.resolve.metadata import editorial_fields, keywords_field, patch_metadata


def record(number: int, text: str) -> WireField:
    return WireField(2, 2, encode_wire([WireField(1, 0, number), WireField(2, 2, text.encode())]))


def blob(records: list[WireField]) -> bytes:
    data = encode_wire(records)
    message = encode_wire([WireField(2, 2, struct.pack(">II", 1000001, len(data)) + data)])
    return encode_map({"BlobData": MapField(12, 0, wrap(message, (10001, 129)))})


def records(data: bytes) -> list[WireField]:
    value = decode_map(data)["BlobData"].value
    assert value is not None
    message, mode = unwrap(value)
    assert mode == (10001, 129)
    outer = decode_wire(message)
    assert len(outer) == 1
    assert outer[0].number == 2
    ba = outer[0].value
    assert isinstance(ba, bytes)
    assert struct.unpack(">II", ba[:8]) == (1000001, len(ba) - 8)
    return decode_wire(ba[8:])


def fields(data: bytes) -> dict[int, str]:
    result = {}
    for field in records(data):
        if field.number == 2:
            assert isinstance(field.value, bytes)
            identifier, text = decode_wire(field.value)[:2]
            assert isinstance(identifier.value, int)
            assert isinstance(text.value, bytes)
            result[identifier.value] = text.value.decode()
    return result


def test_patch_preserves_flags_unknown_ids_and_record_fields() -> None:
    flags = WireField(1, 2, struct.pack(">II", 2, 4) + b"\x0a\x02\x08\x08")
    unknown = WireField(500, 5, b"abcd")
    ordinary = WireField(
        2, 2, encode_wire([WireField(1, 0, 64), WireField(2, 2, b"old"), WireField(900, 0, 7)])
    )
    data = blob([flags, record(999, "Keep me"), ordinary, unknown])
    changed = patch_metadata(data, {64: " Café\n東京 ", 88: "Bentley House Exterior"})
    assert fields(changed) == {999: "Keep me", 64: " Café\n東京 ", 88: "Bentley House Exterior"}
    kept = records(changed)
    assert kept[0] == flags
    assert kept[1] == record(999, "Keep me")
    assert kept[3] == unknown
    assert isinstance(kept[2].value, bytes)
    assert decode_wire(kept[2].value)[-1] == WireField(900, 0, 7)


def test_editorial_mapping_keeps_identifiers_whitespace_and_tag_strings() -> None:
    values = ClipAnnotations(
        title="Display name",
        description=" Arrival\nAt house ",
        scene="12B",
        shot="C",
        take="14A",
        keywords=["exterior", "arrival"],
        camera="A",
        location="House\nExterior",
        notes="Notes",
        tag="good-take",
    )
    expected = {
        64: values.description,
        65: "Notes",
        66: "12B",
        67: "C",
        68: "14A",
        74: "exterior,arrival",
        77: "1",
        80: "A",
        88: "House\nExterior",
    }
    assert editorial_fields(values) == expected
    assert fields(patch_metadata(b"", expected)) == expected
    assert editorial_fields(ClipAnnotations()) == {77: "0"}
    assert editorial_fields(ClipAnnotations(tag="rejected")) == {77: "2"}
    assert patch_metadata(b"", {}) == b""


@pytest.mark.parametrize("keyword", ["two,terms", 'a"quote', "line\nbreak", " spaced ", ""])
def test_ambiguous_keywords_fail_without_inventing_escaping(keyword: str) -> None:
    with pytest.raises(ValueError, match="keyword"):
        editorial_fields(ClipAnnotations(keywords=[keyword]))


def test_duplicate_known_ids_are_rejected() -> None:
    with pytest.raises(ValueError, match="metadata"):
        patch_metadata(blob([record(64, "one"), record(64, "two")]), {64: "new"})


@pytest.mark.parametrize(
    "message", [b"bad", b"\x12\x08" + struct.pack(">II", 1000001, 10), b"\x12\x08" + b"\0" * 8]
)
def test_unrecognised_metadata_is_not_replaced(message: bytes) -> None:
    data = encode_map({"BlobData": MapField(12, 0, wrap(message, (10001, 129)))})
    with pytest.raises(ValueError, match="Unsupported Resolve"):
        patch_metadata(data, {64: "new"})


def test_measured_keywords_follow_the_users_own_without_repeating_them() -> None:
    details = ClipAnnotations(keywords=["harbour", "handheld"])
    merged = editorial_fields(details, measured=("Handheld", "Motion blur"))[74]
    assert merged == "harbour,handheld,Motion blur"
    assert editorial_fields(ClipAnnotations(), measured=("Pan or tilt",))[74] == "Pan or tilt"
    assert 74 not in editorial_fields(ClipAnnotations())
    assert keywords_field([], ()) is None
    with pytest.raises(ValueError, match="individual terms"):
        keywords_field([], ("a,b",))
