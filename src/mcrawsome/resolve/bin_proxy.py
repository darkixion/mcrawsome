"""Linked-video metadata measured in a Resolve 21.1.1 native DRB.

Resolve stores the proxy BtVideoInfo in Sm2MpVideoClip.FieldsBlob wire field 2,
under VIDEO_INFO. The original remains in Video/BtVideoInfo. Virtual CinemaDNG
proxy playback and rendering from originals were verified in Resolve 21.1.1.
"""

from __future__ import annotations

import uuid
import xml.etree.ElementTree as ET

from mcrawsome.resolve.bin_codec import (
    MapField,
    WireField,
    decode_map,
    decode_wire,
    encode_map,
    encode_wire,
    unwrap,
)

_ERROR = "Unsupported native Resolve proxy descriptor"


def _raw(info: ET.Element, name: str) -> bytes:
    node = info.find(name)
    if node is None or not node.text:
        raise ValueError(_ERROR)
    return unwrap(bytes.fromhex(node.text))[0]


def _identity(info: ET.Element) -> bytes:
    identity = info.get("DbId", "")
    if str(uuid.UUID(identity)) != identity:
        raise ValueError(_ERROR)
    return identity.encode("utf-16-be")


def _radiometry(info: ET.Element) -> bytes:
    encoded = bytes.fromhex(info.findtext("Radiometry") or "")
    if encoded[:1] == b"\x80":
        payload = encoded[1:]
    else:
        payload, mode = unwrap(encoded)
        if mode != (0, 129):
            raise ValueError(_ERROR)
    if not decode_wire(payload):
        raise ValueError(_ERROR)
    # Resolve's native VIDEO_INFO retains the bare serialisation flag here.
    # Stripping it makes Resolve discard BtRadiometry and can crash its debayer.
    return b"\x80" + payload


def with_video_proxy(data: bytes, info: ET.Element) -> bytes:
    """Add one video-only proxy, retaining every existing clip field verbatim.

    The caller supplies an independently identified, edited BtVideoInfo. Audio
    continues to use the original's existing aligned audio descriptor. Replacing
    pre-existing proxy/audio metadata is deliberately unsupported.
    """
    fields = decode_wire(data)
    if any(field.number == 2 for field in fields):
        raise ValueError("Clip already contains native proxy metadata")
    if (
        info.tag != "BtVideoInfo"
        or info.findtext("Type") != "STANDARD_CLIP"
        or info.findtext("Eye") != "SUB_TRACK_MONO"
    ):
        raise ValueError(_ERROR)
    identity = _identity(info)
    values = {}
    for name, dbtype in (
        ("VideoMetadata", "BtVideoMetadata"),
        ("Time", "BtVideoTime"),
        ("Proxy", "BtVideoProxy"),
        ("Geometry", "BtGeometry"),
    ):
        raw = _raw(info, name)
        if decode_map(raw).get("DbType") != MapField(10, 0, dbtype.encode("utf-16-be")):
            raise ValueError(_ERROR)
        values[name] = raw
    values["Radiometry"] = _radiometry(info)
    encoded_clip = bytes.fromhex(info.findtext("Clip") or "")
    clip, mode = unwrap(encoded_clip)
    if mode not in ((2, 128), (2, 129)) or not decode_wire(clip):
        raise ValueError(_ERROR)
    # BtClip retains its versioned envelope inside VIDEO_INFO. Radiometry has
    # its bare flag, while the Qt maps above are stored without an envelope.
    values["Clip"] = encoded_clip
    # Keep the same field order as Resolve's generated-proxy reference.
    descriptor = {
        "VideoMetadata": MapField(12, 0, values["VideoMetadata"]),
        "UniqueId": MapField(10, 0, identity),
        "Time": MapField(12, 0, values["Time"]),
        "Radiometry": MapField(12, 0, values["Radiometry"]),
        "Proxy": MapField(12, 0, values["Proxy"]),
        "Geometry": MapField(12, 0, values["Geometry"]),
        "DbType": MapField(10, 0, "BtVideoInfo".encode("utf-16-be")),
        "Clip": MapField(12, 0, values["Clip"]),
    }
    proxy = encode_map({"VIDEO_INFO": MapField(12, 0, encode_map(descriptor))})
    return data + encode_wire([WireField(2, 2, proxy)])
