"""Native marker payloads against an actual Resolve-exported reference, plus archive ownership."""

import struct
from pathlib import Path

import pytest
from test_timeline_archive import documents
from test_timeline_archive import media as media

from mcrawsome.resolve.bin_archive import Media
from mcrawsome.resolve.bin_codec import decode_map, decode_wire, unwrap
from mcrawsome.resolve.markers import Marker, encode
from mcrawsome.resolve.timeline_archive import _write


def payload(data: bytes) -> bytes:
    value = decode_map(data)["BlobData"].value
    assert isinstance(value, bytes)
    return unwrap(value)[0]


def unpack(data: bytes) -> list[tuple[int, int, str, str, str]]:
    [outer] = decode_wire(payload(data))
    assert outer.number == 2
    assert isinstance(outer.value, bytes)
    result = []
    for item in decode_wire(outer.value):
        assert isinstance(item.value, bytes)
        first, second = decode_wire(item.value)
        assert isinstance(first.value, int)
        assert isinstance(second.value, bytes)
        version, length = struct.unpack(">II", second.value[:8])
        assert version == 2
        assert length == len(second.value) - 8
        [wrapped] = decode_wire(second.value[8:])
        assert isinstance(wrapped.value, bytes)
        colour, *text = decode_wire(wrapped.value)
        strings = [f.value.decode("utf-8") for f in text if isinstance(f.value, bytes)]
        assert isinstance(colour.value, int)
        assert len(strings) == 3
        result.append((first.value, colour.value, strings[0], strings[1], strings[2]))
    return result


def test_matches_resolve_exported_marker_payload() -> None:
    reference = bytes.fromhex((Path(__file__).parent / "data/markers_19_1.hex").read_text().strip())
    markers = [
        Marker(60, 12, "Red", "MK_BETA", "second note"),
        Marker(24, 1, "Blue", "MK_ALPHA", "first note"),
    ]
    assert payload(encode(markers)) == payload(reference)
    assert unpack(encode(markers)) == [
        (60, 32, "second note", "12", "MK_BETA"),
        (24, 2, "first note", "1", "MK_ALPHA"),
    ]


def test_native_archive_owns_markers_and_offsets_clips_from_timeline_start(
    tmp_path: Path, media: list[Media]
) -> None:
    path = tmp_path / "markers.drt"
    _write(
        path,
        media,
        name="Motion",
        markers=[
            [Marker(0, 12, "Green", "Low motion", "suggestion")],
            [Marker(3, 6, "Blue", "Steady rotation", "suggestion")],
        ],
    )
    project, master, _ = documents(path)
    node = project.find(".//Sm2SequenceLockableBlob")
    sequence = master.find(".//Sm2Sequence")
    assert node is not None
    assert sequence is not None
    assert node.findtext("BlobOwner") == sequence.attrib["DbId"]
    assert unpack(bytes.fromhex(node.findtext("FieldsBlob") or "")) == [
        (27, 2, "suggestion", "6", "Steady rotation"),
        (0, 8, "suggestion", "12", "Low motion"),
    ]


def test_default_archive_has_no_motion_marker_blob(tmp_path: Path, media: list[Media]) -> None:
    path = tmp_path / "plain.drt"
    _write(path, media, name="Plain")
    assert documents(path)[0].find(".//Sm2SequenceLockableBlob") is None


@pytest.mark.parametrize("marker", [Marker(-1, 1, "Red", "bad", ""), Marker(0, 0, "Green", "bad", "")])
def test_invalid_marker_ranges_are_refused(marker: Marker) -> None:
    with pytest.raises(ValueError, match="Invalid motion marker range"):
        encode([marker])


def test_out_of_clip_marker_is_not_published(tmp_path: Path, media: list[Media]) -> None:
    path = tmp_path / "bad.drt"
    with pytest.raises(ValueError, match="outside"):
        _write(path, media, name="Bad", markers=[[Marker(23, 2, "Red", "bad", "")], []])
    assert not path.exists()


def test_review_categories_have_distinct_native_resolve_colours() -> None:
    from mcrawsome.analysis.review_markers import REVIEW_COLOURS

    expected = {
        "Possible shake": ("Red", 32),
        "Frequent focus changes": ("Purple", 128),
        "Recorded tilt": ("Pink", 64),
        "Remaining tilt": ("Cyan", 4),
        "Highlight clipping": ("Yellow", 16),
        "Low recorded signal": ("Blue", 2),
        "Recording gap": ("Sand", 32768),
        "Motion blur": ("Green", 8),
    }
    assert len(set(REVIEW_COLOURS.values())) == 8
    markers = []
    bits = {}
    for index, (name, (colour, native)) in enumerate(expected.items()):
        assert REVIEW_COLOURS[name] == colour
        markers.append(Marker(index, 1, REVIEW_COLOURS[name], name, "review"))
        bits[name] = native
    decoded = unpack(encode(markers))
    assert {name: colour for _, colour, _, _, name in decoded} == bits
