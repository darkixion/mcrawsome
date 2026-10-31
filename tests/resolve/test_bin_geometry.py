"""The native bin must retain the orientation that Resolve reads from its DNGs."""

import json
import struct
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from mcrawsome.resolve.bin_geometry import orient_geometry

_SAMPLE = json.loads((Path(__file__).parent / "data/geometry_21_1.json").read_text())
NORMAL = bytes.fromhex(_SAMPLE["normal"])
ROTATED = bytes.fromhex(_SAMPLE["rotated_180"])


def test_orientation_matches_resolves_native_direct_import() -> None:
    assert orient_geometry(NORMAL, 3) == ROTATED
    assert orient_geometry(ROTATED, 1) == NORMAL
    assert orient_geometry(NORMAL, 1) == NORMAL
    assert orient_geometry(ROTATED, 3) == ROTATED


def _text(value: str) -> bytes:
    encoded = value.encode("utf-16-be")
    return struct.pack(">I", len(encoded)) + encoded


def test_unrelated_geometry_fields_and_unicode_are_preserved() -> None:
    extra = _text("校正🎥") + struct.pack(">IBi", 2, 0, 234)
    normal = NORMAL[:4] + struct.pack(">I", 6) + NORMAL[8:] + extra
    rotated = ROTATED[:4] + struct.pack(">I", 8) + ROTATED[8:] + extra
    assert orient_geometry(normal, 3) == rotated
    assert orient_geometry(rotated, 1) == normal


@pytest.mark.parametrize("orientation", [0, 2, 4, 5, 6, 7, 8, 180])
def test_unverified_orientations_are_refused(orientation: int) -> None:
    with pytest.raises(ValueError, match="orientation"):
        orient_geometry(NORMAL, orientation)


def test_wrong_object_duplicate_keys_and_unsupported_types_are_refused() -> None:
    for value in (
        NORMAL.replace(_text("BtGeometry"), _text("BtVideoTime")),
        NORMAL.replace(_text("ScanType"), _text("UniqueId")),
        NORMAL.replace(_text("ScanType") + struct.pack(">I", 2), _text("ScanType") + struct.pack(">I", 99)),
        NORMAL[:4] + struct.pack(">I", 0xFFFFFFFF) + NORMAL[8:],
        ROTATED.replace(
            _text("SrcVFlip") + bytes.fromhex("000000010001"),
            _text("SrcVFlip") + bytes.fromhex("000000010002"),
        ),
        NORMAL + b"trailing",
        b"\0" * (65536 + 1),
    ):
        with pytest.raises(ValueError, match="geometry"):
            orient_geometry(value, 3)


@given(st.integers(min_value=0, max_value=len(ROTATED) - 1))
def test_truncated_native_data_is_refused(end: int) -> None:
    with pytest.raises(ValueError, match="geometry"):
        orient_geometry(ROTATED[:end], 3)


@given(st.binary(max_size=512))
def test_unrecognised_data_fails_without_unbounded_reads(data: bytes) -> None:
    with pytest.raises(ValueError, match="geometry"):
        orient_geometry(data, 3)
