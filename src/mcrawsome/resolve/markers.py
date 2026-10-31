"""Native timeline markers. Format measured by the MIT-licensed DaVinci Resolve MCP project.

See packaging/LICENCES/DaVinci-Resolve-MCP.txt and tests/resolve/data/markers_19_1.hex.
The independent Python encoder reuses MCRAWsome's bounded Qt/protobuf/zstd primitives.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, replace
from typing import Literal
from xml.etree import ElementTree as ET

from mcrawsome.resolve.bin_codec import MapField, WireField, encode_map, encode_wire, wrap

MarkerColour = Literal["Green", "Blue", "Red", "Cyan", "Yellow", "Pink", "Purple", "Sand"]
# Native bits from the Resolve 19.1 live-export reference (timeline-markers-blob.js).
COLOUR_BITS: dict[MarkerColour, int] = {
    "Blue": 2,
    "Cyan": 4,
    "Green": 8,
    "Yellow": 16,
    "Red": 32,
    "Pink": 64,
    "Purple": 128,
    "Sand": 32768,
}


@dataclass(frozen=True)
class Marker:
    frame: int
    duration: int
    colour: MarkerColour
    name: str
    note: str


def encode(markers: list[Marker]) -> bytes:
    if len(markers) > 4096 or len({m.frame for m in markers}) != len(markers):
        raise ValueError("Too many motion markers or duplicate marker positions")
    entries = []
    for marker in sorted(markers, key=lambda m: m.frame, reverse=True):
        if marker.frame < 0 or marker.duration < 1:
            raise ValueError("Invalid motion marker range")
        text = [marker.note, str(marker.duration), marker.name]
        if any(len(s.encode("utf-8")) > 4096 for s in text):
            raise ValueError("Motion marker text is too long")
        body = encode_wire(
            [
                WireField(1, 0, COLOUR_BITS[marker.colour]),
                *(WireField(3, 2, s.encode("utf-8")) for s in text),
            ]
        )
        inner = encode_wire([WireField(1, 2, body)])
        wrapped = struct.pack(">II", 2, len(inner)) + inner
        entry = encode_wire([WireField(1, 0, marker.frame), WireField(2, 2, wrapped)])
        entries.append(WireField(1, 2, entry))
    payload = encode_wire([WireField(2, 2, encode_wire(entries))])
    return encode_map({"BlobData": MapField(12, 0, wrap(payload, (10001, 129)))})


def attach(locks: ET.Element, owner: str, groups: list[list[Marker]], lengths: list[int]) -> None:
    import uuid

    if len(groups) != len(lengths):
        raise ValueError("Expected one motion marker group per clip")
    markers = []
    offset = 0
    for group, count in zip(groups, lengths, strict=True):
        for marker in group:
            if marker.frame < 0 or marker.frame + marker.duration > count:
                raise ValueError("Motion marker lies outside its timeline clip")
            markers.append(replace(marker, frame=marker.frame + offset))
        offset += count
    if markers:
        node = ET.SubElement(
            ET.SubElement(locks, "Element"), "Sm2SequenceLockableBlob", DbId=str(uuid.uuid4())
        )
        ET.SubElement(node, "FieldsBlob").text = encode(markers).hex()
        ET.SubElement(node, "BlobOwner").text = owner
        ET.SubElement(node, "DbSavedTime").text = "0"
