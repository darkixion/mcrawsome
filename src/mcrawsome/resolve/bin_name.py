"""CinemaDNG display-name override measured from Resolve 21.1.1 rename exports."""

from __future__ import annotations

from mcrawsome.resolve.bin_codec import WireField, decode_wire, encode_wire, patch_wire

_ERROR = "Unsupported Resolve clip-name field"


def _name(data: bytes, title: str, depth: int) -> bytes:
    fields = decode_wire(data)
    if depth:
        targets = [field for field in fields if field.number == 1]
        if len(targets) != 1 or targets[0].kind != 2 or not isinstance(targets[0].value, bytes):
            raise ValueError(_ERROR)
        return patch_wire(data, {1: _name(targets[0].value, title, depth - 1)})
    codecs = [field for field in fields if field.number == 6]
    names = [field for field in fields if field.number == 2]
    if codecs != [WireField(6, 0, 4)] or len(names) > 1 or any(field.kind != 2 for field in names):
        raise ValueError(_ERROR)
    if not title.strip():
        return encode_wire([field for field in fields if field.number != 2])
    if names:
        return patch_wire(data, {2: title.encode("utf-8")})
    return encode_wire([WireField(2, 2, title.encode("utf-8")), *fields])


def patch_clip_name(data: bytes, title: str) -> bytes:
    """Patch wire path 1/1/1/2 while retaining the DNG codec and unrelated clip properties."""
    return _name(data, title, 3)


def patch_audio_name(data: bytes, title: str) -> bytes:
    """Audio overrides use path 1/1/2; the unrenamed scaffold has a native empty stub."""
    outer = next(field.value for field in decode_wire(data) if field.number == 1)
    if not isinstance(outer, bytes):
        raise ValueError(_ERROR)
    properties = next(field.value for field in decode_wire(outer) if field.number == 1)
    if not isinstance(properties, bytes):
        raise ValueError(_ERROR)
    if decode_wire(properties) == [WireField(1, 0, 0)]:
        if not title.strip():
            return data
        data = patch_wire(data, {1: patch_wire(outer, {1: encode_wire([WireField(6, 0, 4)])})})
    return _name(data, title, 2)
