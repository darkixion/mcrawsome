"""Bounded codecs for the measured Resolve 21.1 native archive fields.

Unknown wire and Qt values keep their bytes. These are private archive building blocks, not a
parser for arbitrary Resolve projects.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import struct
from dataclasses import dataclass
from functools import lru_cache

_LIMIT = 8 << 20
_ERROR = "Unsupported Resolve archive field"


class Reader:
    def __init__(self, data: bytes) -> None:
        if len(data) > _LIMIT:
            raise ValueError(_ERROR)
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

    def varint(self) -> int:
        value = 0
        for shift in range(0, 70, 7):
            byte = self.take(1)[0]
            if shift == 63 and byte > 1:
                break
            value |= (byte & 127) << shift
            if byte < 128:
                return value
        raise ValueError(_ERROR)


def integer(value: int) -> bytes:
    if not 0 <= value < 1 << 64:
        raise ValueError(_ERROR)
    result = bytearray()
    while value > 127:
        result.append((value & 127) | 128)
        value >>= 7
    result.append(value)
    return bytes(result)


@dataclass(frozen=True)
class WireField:
    number: int
    kind: int
    value: int | bytes


def decode_wire(data: bytes) -> list[WireField]:
    reader = Reader(data)
    result: list[WireField] = []
    while reader.position < len(data):
        tag = reader.varint()
        number, kind = tag >> 3, tag & 7
        if not 1 <= number < 1 << 29 or len(result) >= 4096:
            raise ValueError(_ERROR)
        if kind == 0:
            value: int | bytes = reader.varint()
        elif kind == 2:
            value = reader.take(reader.varint())
        elif kind in (1, 5):
            value = reader.take(8 if kind == 1 else 4)
        else:
            raise ValueError(_ERROR)
        result.append(WireField(number, kind, value))
    return result


def encode_wire(fields: list[WireField]) -> bytes:
    result = bytearray()
    for field in fields:
        result.extend(integer(field.number * 8 + field.kind))
        if isinstance(field.value, int):
            if field.kind != 0:
                raise ValueError(_ERROR)
            result.extend(integer(field.value))
        else:
            if field.kind == 2:
                result.extend(integer(len(field.value)))
            result.extend(field.value)
    return bytes(result)


def patch_wire(data: bytes, updates: dict[int, int | bytes]) -> bytes:
    fields = decode_wire(data)
    if any(sum(field.number == number for field in fields) != 1 for number in updates):
        raise ValueError(_ERROR)
    return encode_wire([WireField(f.number, f.kind, updates.get(f.number, f.value)) for f in fields])


@dataclass(frozen=True)
class MapField:
    kind: int
    nullable: int
    value: bytes | None


def decode_map(data: bytes) -> dict[str, MapField]:
    reader = Reader(data)
    if reader.integer() != 1:
        raise ValueError(_ERROR)
    count = reader.integer()
    if count > 4096:
        raise ValueError(_ERROR)
    result = {}
    for _ in range(count):
        key = reader.sized()
        if not key:
            raise ValueError(_ERROR)
        name = key.decode("utf-16-be")
        kind, nullable = reader.integer(), reader.take(1)[0]
        if name in result or nullable not in (0, 1):
            raise ValueError(_ERROR)
        if kind in (10, 12):
            value = reader.sized()
        else:
            size = {1: 1, 2: 4, 3: 4, 4: 8, 5: 8, 6: 8}.get(kind)
            if size is None:
                raise ValueError(_ERROR)
            value = reader.take(size)
            if kind == 1 and value not in (b"\0", b"\1"):
                raise ValueError(_ERROR)
        result[name] = MapField(kind, nullable, value)
    if reader.position != len(data):
        raise ValueError(_ERROR)
    return result


def _sized(value: bytes | None) -> bytes:
    return b"\xff" * 4 if value is None else struct.pack(">I", len(value)) + value


def encode_map(fields: dict[str, MapField]) -> bytes:
    result = bytearray(struct.pack(">II", 1, len(fields)))
    for name, field in fields.items():
        result.extend(_sized(name.encode("utf-16-be")))
        result.extend(struct.pack(">IB", field.kind, field.nullable))
        result.extend(_sized(field.value) if field.kind in (10, 12) else field.value or b"")
    return bytes(result)


def patch_map(data: bytes, updates: dict[str, bytes]) -> bytes:
    fields = decode_map(data)
    if not updates.keys() <= fields.keys():
        raise ValueError(_ERROR)
    return encode_map({k: MapField(f.kind, f.nullable, updates.get(k, f.value)) for k, f in fields.items()})


@lru_cache(maxsize=1)
def _zstd() -> ctypes.CDLL:
    name = ctypes.util.find_library("zstd")
    if name is None:
        raise ValueError("Resolve archives require libzstd")
    library = ctypes.CDLL(name)
    signatures = [
        ("ZSTD_getFrameContentSize", ctypes.c_ulonglong, [ctypes.c_void_p, ctypes.c_size_t]),
        (
            "ZSTD_decompress",
            ctypes.c_size_t,
            [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t],
        ),
        ("ZSTD_compressBound", ctypes.c_size_t, [ctypes.c_size_t]),
        (
            "ZSTD_compress",
            ctypes.c_size_t,
            [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int],
        ),
        ("ZSTD_isError", ctypes.c_uint, [ctypes.c_size_t]),
    ]
    for symbol, restype, argtypes in signatures:
        function = getattr(library, symbol)
        function.restype = restype
        function.argtypes = argtypes
    return library


def _compress(data: bytes) -> bytes:
    library = _zstd()
    output = ctypes.create_string_buffer(library.ZSTD_compressBound(len(data)))
    size = int(library.ZSTD_compress(output, len(output), data, len(data), 3))
    if library.ZSTD_isError(size):
        raise ValueError(_ERROR)
    return output.raw[:size]


def _decompress(data: bytes) -> bytes:
    library = _zstd()
    size = int(library.ZSTD_getFrameContentSize(data, len(data)))
    if not 0 < size <= _LIMIT:
        raise ValueError(_ERROR)
    output = ctypes.create_string_buffer(size)
    actual = int(library.ZSTD_decompress(output, size, data, len(data)))
    if library.ZSTD_isError(actual) or actual != size:
        raise ValueError(_ERROR)
    return output.raw[:actual]


def unwrap(data: bytes) -> tuple[bytes, tuple[int, int]]:
    if len(data) > _LIMIT:
        raise ValueError(_ERROR)
    if data[:4] in (b"\0\0\0\1", b"\0\0\0\2", b"\0\0\x27\x11") and len(data) >= 9:
        version, size = struct.unpack(">II", data[:8])
        if data[8] in (128, 129):
            if size != len(data) - 8:
                raise ValueError(_ERROR)
            return (_decompress(data[9:]) if data[8] == 129 else data[9:]), (version, data[8])
    if data[:5] == b"\x81\x28\xb5\x2f\xfd":
        return _decompress(data[1:]), (0, 129)
    return data, (0, 0)


def wrap(data: bytes, mode: tuple[int, int]) -> bytes:
    if len(data) > _LIMIT:
        raise ValueError(_ERROR)
    version, flag = mode
    if mode == (0, 0):
        return data
    if mode not in ((0, 129), (1, 128), (1, 129), (2, 128), (2, 129), (10001, 128), (10001, 129)):
        raise ValueError(_ERROR)
    payload = bytes([flag]) + (_compress(data) if flag == 129 else data)
    return (struct.pack(">II", version, len(payload)) if version else b"") + payload
