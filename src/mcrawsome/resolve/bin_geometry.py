"""Orientation in Resolve 21.1's native bin geometry, measured against direct DNG import.

This is one part of the standalone bin writer, not a complete DRB archive generator. The version-1
Qt map preserves unknown supported fields verbatim. Other object layouts and unverified rotations
are refused rather than producing a bin with the wrong orientation.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

_ERROR = "Unsupported Resolve bin geometry"
_FLIPS = ("SrcVFlip", "SrcHFlip")


@dataclass(frozen=True)
class _Field:
    name: str
    kind: int
    value: bytes | None
    encoded: bytes


class _Reader:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.position = 0

    def take(self, size: int) -> bytes:
        end = self.position + size
        if size < 0 or end > len(self.data):
            raise ValueError(_ERROR)
        value = self.data[self.position : end]
        self.position = end
        return value

    def integer(self) -> int:
        return int.from_bytes(self.take(4), "big")

    def sized(self) -> bytes | None:
        size = self.integer()
        return None if size == 0xFFFFFFFF else self.take(size)

    def field(self) -> _Field:
        start = self.position
        key = self.sized()
        if not key:
            raise ValueError(_ERROR)
        try:
            name = key.decode("utf-16-be")
        except UnicodeError as exc:
            raise ValueError(_ERROR) from exc
        kind, nullable = self.integer(), self.take(1)[0]
        if nullable not in (0, 1):
            raise ValueError(_ERROR)
        if kind in (10, 12):  # QString or QByteArray: length in bytes, not characters
            value = self.sized()
            if kind == 10 and value is not None:
                try:
                    value.decode("utf-16-be")
                except UnicodeError as exc:
                    raise ValueError(_ERROR) from exc
        else:
            size = {1: 1, 2: 4, 3: 4, 4: 8, 5: 8, 6: 8}.get(kind)
            if size is None:
                raise ValueError(_ERROR)
            value = self.take(size)
            if kind == 1 and value not in (b"\0", b"\1"):
                raise ValueError(_ERROR)
        return _Field(name, kind, value, self.data[start : self.position])


def _flip(name: str) -> bytes:
    key = name.encode("utf-16-be")
    return struct.pack(">I", len(key)) + key + struct.pack(">IBB", 1, 0, 1)


def orient_geometry(data: bytes, orientation: int) -> bytes:
    """Set the native source flips for DNG TIFF Orientation 1 or 3 without changing media pixels.

    Normal geometry omits the two optional flags. A 180-degree source has both flags enabled, as
    Resolve's own direct import records it. All sizes, scan settings and unrelated fields survive.
    """
    if orientation not in (1, 3):
        raise ValueError(
            "Native Resolve archives require recorded-orientation media. "
            "Send again from the Library to apply the saved turn through Clip Attributes."
        )
    if not 8 <= len(data) <= 65536:
        raise ValueError(_ERROR)
    reader = _Reader(data)
    if reader.integer() != 1:
        raise ValueError(_ERROR)
    count = reader.integer()
    if not 1 <= count <= 128:
        raise ValueError(_ERROR)
    fields = [reader.field() for _ in range(count)]
    names = {field.name: field for field in fields}
    db_type = names.get("DbType")
    if (
        reader.position != len(data)
        or len(names) != count
        or "UniqueId" not in names
        or db_type is None
        or db_type.kind != 10
        or db_type.value != "BtGeometry".encode("utf-16-be")
        or any(field.name in _FLIPS and field.kind != 1 for field in fields)
    ):
        raise ValueError(_ERROR)
    encoded = []
    for field in fields:
        if field.name in _FLIPS:
            continue
        encoded.append(field.encoded)
        if field.name == "UniqueId" and orientation == 3:
            encoded.extend(_flip(name) for name in _FLIPS)
    return struct.pack(">II", 1, len(encoded)) + b"".join(encoded)
