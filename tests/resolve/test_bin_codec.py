import struct

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

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


@given(st.binary(min_size=1, max_size=2048))
@example(b"\x81\x28\xb5\x2f\xfd")
def test_native_envelopes_are_lossless(data: bytes) -> None:
    # Unwrapped fields have no escaping: arbitrary bytes can start with an envelope marker.
    assert wrap(data, (0, 0)) == data
    for mode in ((0, 129), (1, 128), (1, 129), (2, 128), (2, 129), (10001, 128), (10001, 129)):
        assert unwrap(wrap(data, mode)) == (data, mode)


@pytest.mark.parametrize("data", [b"\x80" * 10, b"\x0a\x80", b"\x0a\x03x", b"\x0f", b"\x09xx", b"\x00"])
def test_wire_refuses_truncation_and_invalid_tags(data: bytes) -> None:
    with pytest.raises(ValueError, match="Unsupported Resolve archive field"):
        decode_wire(data)


def test_wire_retains_unknown_fixed_and_repeated_fields() -> None:
    fields = [
        WireField(1, 2, b"path"),
        WireField(500, 0, 2**63),
        WireField(500, 5, b"1234"),
        WireField(42, 1, b"12345678"),
    ]
    assert decode_wire(encode_wire(fields)) == fields


def test_qt_map_preserves_null_empty_and_unknown_values() -> None:
    fields = {
        "null": MapField(12, 0, None),
        "empty": MapField(10, 1, b""),
        "unsigned": MapField(5, 0, struct.pack(">Q", 2**63)),
        "bool": MapField(1, 0, b"\1"),
    }
    assert decode_map(encode_map(fields)) == fields


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"\0" * 8,
        struct.pack(">II", 1, 4097),
        struct.pack(">II", 2, 8) + b"\x81bad",
        b"\x81\x28\xb5\x2f\xfdbad",
    ],
)
def test_rejects_bad_lengths_and_compression(data: bytes) -> None:
    with pytest.raises(ValueError, match="Unsupported Resolve archive field"):
        decode_map(unwrap(data)[0])
