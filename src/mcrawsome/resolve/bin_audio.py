"""Synced WAV tracks measured from Resolve's timecode Auto Sync Audio bin export."""

import struct

from mcrawsome.resolve.bin_codec import (
    MapField,
    WireField,
    decode_map,
    decode_wire,
    encode_map,
    encode_wire,
    patch_wire,
)


def _section(raw: bytes, number: int) -> bytes:
    fields = [field for field in decode_wire(raw) if field.number == number]
    if len(fields) != 1 or fields[0].kind != 2 or not isinstance(fields[0].value, bytes):
        raise ValueError("Unsupported synced audio field")
    return fields[0].value


def sync_tracks(video: bytes, audio: bytes) -> bytes:
    """Reference the retained WAV source; never embed a duplicate source object in the video."""
    tracks = decode_map(_section(_section(audio, 1), 5))
    track = decode_map(tracks["0"].value or b"")
    # Origin 1 is Resolve's synced external audio, with matching zero-based media extents.
    revised = {}
    for key, value in track.items():
        if key != "Origin":
            revised[key] = value
        if key == "Type":
            revised["Origin"] = MapField(3, 0, struct.pack(">I", 1))
    tracks["0"] = MapField(12, 0, encode_map(revised))
    one = _section(video, 1)
    inner = _section(one, 1)
    fields = [field for field in decode_wire(inner) if field.number != 5]
    inner = encode_wire([*fields, WireField(5, 2, encode_map(tracks))])
    return patch_wire(video, {1: patch_wire(one, {1: inner})})


def virtual_tracks(channels: int) -> bytes:
    if channels not in (1, 2):
        raise ValueError("Synced audio requires mono or stereo WAV")
    values = struct.pack(">II", 2, channels)
    values += b"".join(struct.pack(">I", 0x20004001 + index) for index in range(channels))
    track = encode_map(
        {
            "ChannelsBA": MapField(12, 0, values),
            "AudioType": MapField(2, 0, struct.pack(">i", 1 if channels == 1 else 0)),
        }
    )
    return encode_map({"0": MapField(12, 0, track)})
