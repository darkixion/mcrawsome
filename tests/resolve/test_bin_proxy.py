"""Native serialiser evidence; live CinemaDNG proxy acceptance is a separate gate."""

import json
import struct
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from mcrawsome.resolve.bin_codec import (
    WireField,
    decode_map,
    decode_wire,
    encode_wire,
    unwrap,
    wrap,
)
from mcrawsome.resolve.bin_proxy import with_video_proxy


def reference() -> tuple[bytes, ET.Element]:
    fixture = json.loads((Path(__file__).parent / "data/linked_proxy_21_1.json").read_text())
    raw = bytes.fromhex(fixture["video_info"])
    fields = decode_map(raw)
    assert fields["UniqueId"].value is not None
    info = ET.Element("BtVideoInfo", DbId=fields["UniqueId"].value.decode("utf-16-be"))
    for key, field in fields.items():
        if key not in ("UniqueId", "DbType"):
            assert field.value is not None
            if key == "Radiometry":
                # Actual XML uses a bare compressed flag, not a versioned wrapper
                # around an already flagged value. This distinction caused the crash.
                assert field.value[:1] == b"\x80"
                encoded = wrap(field.value[1:], (0, 129))
            else:
                encoded = field.value if key == "Clip" else wrap(field.value, (2, 129))
            ET.SubElement(info, key).text = encoded.hex()
    ET.SubElement(info, "Type").text = "STANDARD_CLIP"
    ET.SubElement(info, "Eye").text = "SUB_TRACK_MONO"
    return raw, info


def test_video_proxy_matches_native_reference_and_preserves_original_fields() -> None:
    expected, info = reference()
    original = encode_wire(
        [WireField(1, 2, b"original name, RAW properties and audio"), WireField(42, 5, b"1234")]
    )
    before = ET.tostring(info)
    result = with_video_proxy(original, info)
    assert result.startswith(original)
    fields = decode_wire(result)
    assert fields[:-1] == decode_wire(original)
    assert fields[-1].number == 2
    assert isinstance(fields[-1].value, bytes)
    metadata = decode_map(fields[-1].value)
    assert list(metadata) == ["VIDEO_INFO"]
    assert metadata["VIDEO_INFO"].value == expected
    assert ET.tostring(info) == before


def test_proxy_descriptor_keeps_its_own_geometry_and_media_path() -> None:
    _, info = reference()
    result = decode_wire(with_video_proxy(b"", info))[0].value
    assert isinstance(result, bytes)
    descriptor = decode_map(decode_map(result)["VIDEO_INFO"].value or b"")
    geometry = decode_map(descriptor["Geometry"].value or b"")
    assert geometry["Resolution"].value == struct.pack(">QQ", 1024, 576)
    clip = {field.number: field.value for field in decode_wire(unwrap(descriptor["Clip"].value or b"")[0])}
    assert clip[1] == b"/native/proxy"
    assert clip[2] == b"sample.mov"
    assert clip[5] == b"apch"
    assert unwrap(bytes.fromhex(info.findtext("Geometry") or ""))[1] == (2, 129)


def test_existing_proxy_metadata_is_never_overwritten() -> None:
    _, info = reference()
    original = encode_wire([WireField(2, 2, b"existing proxy or embedded proxy audio")])
    with pytest.raises(ValueError, match="already contains"):
        with_video_proxy(original, info)


def test_bare_uncompressed_radiometry_keeps_its_serialization_flag() -> None:
    expected, info = reference()
    radiometry = info.find("Radiometry")
    assert radiometry is not None
    value = decode_map(expected)["Radiometry"].value
    assert value is not None
    radiometry.text = value.hex()
    result = decode_wire(with_video_proxy(b"", info))[0].value
    assert isinstance(result, bytes)
    assert decode_map(result)["VIDEO_INFO"].value == expected


def test_unwrapped_radiometry_that_crashed_resolve_is_rejected() -> None:
    _, info = reference()
    radiometry = info.find("Radiometry")
    assert radiometry is not None
    radiometry.text = unwrap(bytes.fromhex(radiometry.text or ""))[0].hex()
    with pytest.raises(ValueError, match="Unsupported native Resolve proxy"):
        with_video_proxy(b"", info)


def test_native_raw_xml_radiometry_preserves_its_bayer_properties() -> None:
    fixture = json.loads((Path(__file__).parent / "data/linked_proxy_21_1.json").read_text())
    _, info = reference()
    radiometry = info.find("Radiometry")
    assert radiometry is not None
    radiometry.text = fixture["native_raw_xml_radiometry"]
    original, mode = unwrap(bytes.fromhex(radiometry.text or ""))
    assert mode == (0, 129)
    result = decode_wire(with_video_proxy(b"", info))[0].value
    assert isinstance(result, bytes)
    descriptor = decode_map(decode_map(result)["VIDEO_INFO"].value or b"")
    encoded = descriptor["Radiometry"].value
    assert encoded == b"\x80" + original
    assert encoded is not None
    properties = {field.number: field.value for field in decode_wire(encoded[1:])}
    assert properties[1] == 312
    assert properties[5] == 32


def test_unwrapped_clip_descriptor_is_rejected() -> None:
    _, info = reference()
    clip = info.find("Clip")
    assert clip is not None
    clip.text = unwrap(bytes.fromhex(clip.text or ""))[0].hex()
    with pytest.raises(ValueError, match="Unsupported native Resolve proxy"):
        with_video_proxy(b"", info)


@pytest.mark.parametrize("field", ["Time", "Geometry", "Proxy", "VideoMetadata", "Radiometry", "Clip"])
def test_incomplete_proxy_descriptor_is_rejected(field: str) -> None:
    _, info = reference()
    element = info.find(field)
    assert element is not None
    info.remove(element)
    with pytest.raises(ValueError, match="Unsupported native Resolve proxy"):
        with_video_proxy(b"", info)


@pytest.mark.parametrize("field", ["Type", "Eye"])
def test_unmeasured_clip_types_are_rejected(field: str) -> None:
    _, info = reference()
    element = info.find(field)
    assert element is not None
    element.text = "UNMEASURED"
    with pytest.raises(ValueError, match="Unsupported native Resolve proxy"):
        with_video_proxy(b"", info)
