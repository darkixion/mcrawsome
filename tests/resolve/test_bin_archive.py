"""Native bin handoff uses virtual file metadata and never decodes or exports frames."""

import json
import struct
import uuid
import xml.etree.ElementTree as ET
from collections.abc import Callable
from pathlib import Path
from typing import Any
from zipfile import ZipFile

import pytest

from mcrawsome.io.clip import Clip
from mcrawsome.io.metadata import FrameMetadata
from mcrawsome.library.annotations import ClipAnnotations, read_annotations, write_annotations
from mcrawsome.library.scan import clip_id
from mcrawsome.mount.manager import MountedSequence
from mcrawsome.mount.sequence import Sequence
from mcrawsome.resolve.bin_archive import mounted_media, write_bin
from mcrawsome.resolve.bin_codec import MapField, decode_map, decode_wire, unwrap
from mcrawsome.resolve.timeline_archive import write_timeline


def mounted(source: Path, folder: Path, timecode: str | None = None) -> MountedSequence:
    folder.mkdir()
    with Sequence(source, timecode=timecode) as sequence:
        for name in sequence.names():
            # Files contain headers and sparse payload space, not developed or decoded pixels.
            header = (
                sequence.wave.header if name.endswith(".wav") and sequence.wave else sequence._header(0)[0]
            )
            with (folder / name).open("wb") as stream:
                stream.write(header)
                stream.truncate(sequence.size(name))
        return MountedSequence(
            id=uuid.uuid4().hex[:24],
            source=source,
            path=folder,
            stamp=sequence.stamp,
            frames=len(sequence.sources),
            active=True,
            serving=True,
            timecode=timecode,
        )


def test_unreadable_virtual_header_stops_export_before_publication(tiny_raw: Path, tmp_path: Path) -> None:
    mount = mounted(tiny_raw, tmp_path / "media")
    (mount.path / "tiny-000002.dng").write_bytes(b"invalid TIFF")
    output = tmp_path / "offline.drb"
    with pytest.raises(ValueError, match=r"Virtual media is not readable.*Remount"):
        write_bin(output, [mount], name="Offline")
    assert not output.exists()


def trees(path: Path) -> list[ET.Element]:
    with ZipFile(path) as archive:
        assert archive.testzip() is None
        assert len(archive.namelist()) == 3
        return [
            ET.fromstring(archive.read(name).decode().replace("ListMgt::", "ListMgt__"))
            for name in archive.namelist()
        ]


@pytest.mark.parametrize("turn", [0, 90, 180, 270])
def test_bin_uses_native_clip_attributes_without_rotating_raw_media(
    tiny_raw: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, turn: int
) -> None:
    mount = mounted(tiny_raw, tmp_path / "media")
    before = tiny_raw.read_bytes()
    header = (mount.path / "tiny-000000.dng").read_bytes()
    monkeypatch.setattr(Clip, "raw_frame", lambda *_: pytest.fail("bin rotation decoded RAW"))
    ordinary, rotated = tmp_path / "ordinary.drb", tmp_path / "rotated.drb"
    write_bin(ordinary, [mount], name="Ordinary")
    write_bin(rotated, [mount], name="Rotated", rotations=[turn])
    baseline = next(node for tree in trees(ordinary) for node in tree.iter("BtVideoInfo"))
    actual = next(node for tree in trees(rotated) for node in tree.iter("BtVideoInfo"))
    original_fields = decode_wire(raw(baseline, "Radiometry"))
    fields = decode_wire(raw(actual, "Radiometry"))
    assert next(f.value for f in fields if f.number == 19) == turn * 2
    assert [f for f in fields if f.number != 19] == [f for f in original_fields if f.number != 19]
    actual_geometry = decode_map(raw(actual, "Geometry"))
    baseline_geometry = decode_map(raw(baseline, "Geometry"))
    assert actual_geometry.pop("UniqueId") != baseline_geometry.pop("UniqueId")
    assert actual_geometry == baseline_geometry
    assert tiny_raw.read_bytes() == before
    assert (mount.path / "tiny-000000.dng").read_bytes() == header
    assert mount.raw_settings.rotation == 0


@pytest.mark.parametrize("rotations", [[], [90, 270], [45]])
def test_bin_refuses_invalid_rotation_lists_before_reading_media(
    tiny_raw: Path, tmp_path: Path, rotations: list[int]
) -> None:
    mount = mounted(tiny_raw, tmp_path / "media")
    output = tmp_path / "bad.drb"
    with pytest.raises(ValueError, match="quarter-turn"):
        write_bin(output, [mount], name="Bad", rotations=rotations)
    assert not output.exists()


def test_bin_refuses_applying_a_clip_turn_to_already_rotated_media(tiny_raw: Path, tmp_path: Path) -> None:
    mount = mounted(tiny_raw, tmp_path / "media")
    mount.raw_settings = mount.raw_settings.model_copy(update={"rotation": 90})
    with pytest.raises(ValueError, match="recorded-orientation"):
        write_bin(tmp_path / "twice.drb", [mount], name="Bad", rotations=[90])


def raw(node: ET.Element, name: str) -> bytes:
    return unwrap(bytes.fromhex(node.findtext(name) or ""))[0]


def metadata_fields(node: ET.Element) -> dict[int, str]:
    value = decode_map(bytes.fromhex(node.findtext("FieldsBlob") or ""))["BlobData"].value
    assert value is not None
    outer = decode_wire(unwrap(value)[0])[0].value
    assert isinstance(outer, bytes)
    assert struct.unpack(">II", outer[:8]) == (1000001, len(outer) - 8)
    fields = {}
    for field in decode_wire(outer[8:]):
        assert isinstance(field.value, bytes)
        key, text = decode_wire(field.value)
        assert isinstance(key.value, int)
        assert isinstance(text.value, bytes)
        fields[key.value] = text.value.decode()
    return fields


@pytest.mark.parametrize("writer", [write_bin, write_timeline])
def test_saved_details_reach_the_matching_video_only_and_keep_media_paths(
    tiny_raw: Path,
    make_mcraw: Callable[..., Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    writer: Callable[..., object],
) -> None:
    second = make_mcraw(tmp_path / "second.mcraw", [0, 41666667, 83333334, 125000001])
    mounts = [mounted(tiny_raw, tmp_path / "first"), mounted(second, tmp_path / "second")]
    # Literal UUIDs are user text, even where they look like object identifiers.
    literal = "00000000-0000-4000-8000-000000000001"
    values = [
        ClipAnnotations(
            title=f"Café {literal}",
            description=f" Arrival\n東京 {literal} ",
            keywords=["exterior", "arrival"],
            scene="12B",
            shot="C",
            take="14A",
            location="Bentley House Exterior",
            camera="A",
            notes="Clean sound",
            tag="good-take",
        ),
        ClipAnnotations(title="Second clip", description="Second description", take="15B", tag="rejected"),
    ]
    for mount, details in zip(mounts, values, strict=True):
        write_annotations(mount.source, clip_id(mount.source), details, "")
    monkeypatch.setattr(Clip, "raw_frame", lambda *_: pytest.fail("archive must not decode pixels"))
    monkeypatch.setattr(Clip, "audio", lambda *_: pytest.fail("archive must not join PCM"))
    path = tmp_path / "annotated.zip"
    writer(path, mounts, name="Editorial probe")
    document = trees(path)
    locks = {
        node.findtext("BlobOwner"): node for tree in document for node in tree.iter("BtMetadataLockableBlob")
    }
    videos = [node for tree in document for node in tree.iter("Sm2MpVideoClip")]
    assert [node.findtext("Name") for node in videos] == [item.title for item in values]
    for mount, details, video in zip(mounts, values, videos, strict=True):
        properties = raw(video, "FieldsBlob")
        for _ in range(3):
            value = next(f.value for f in decode_wire(properties) if f.number == 1)
            assert isinstance(value, bytes)
            properties = value
        assert (
            next((f.value for f in decode_wire(properties) if f.number == 2), None) == details.title.encode()
        )
        info = video.find("Video/BtVideoInfo")
        assert info is not None
        fields = metadata_fields(locks[info.attrib["DbId"]])
        assert fields[64] == details.description
        assert fields[68] == details.take
        assert fields[77] == {"untagged": "0", "good-take": "1", "rejected": "2"}[details.tag]
        descriptor = {f.number: f.value for f in decode_wire(raw(info, "Clip"))}
        assert descriptor[1] == str(mount.path).encode()
        assert descriptor[2] == f"{mount.source.stem}-[000000-{mount.frames - 1:06d}].dng".encode()
        assert read_annotations(mount.source, clip_id(mount.source)).values == details
    first_info = videos[0].find("Video/BtVideoInfo")
    assert first_info is not None
    first_fields = metadata_fields(locks[first_info.attrib["DbId"]])
    assert {key: first_fields[key] for key in (64, 65, 66, 67, 68, 74, 77, 80, 88)} == {
        64: values[0].description,
        65: "Clean sound",
        66: "12B",
        67: "C",
        68: "14A",
        74: "exterior,arrival",
        77: "1",
        80: "A",
        88: "Bentley House Exterior",
    }
    assert first_fields[96] == "Google"
    assert first_fields[97] == "Pixel 10 Pro XL"
    assert first_fields[99] == "0/9"
    assert first_fields[107] == "36"
    assert first_fields[220] == "48"
    assert "35 mm equivalent" in first_fields[118]
    for tree in document:
        for audio in tree.iter("Sm2MpAudioClip"):
            info = audio.find("EmbeddedAudioVec/Element/BtAudioInfo")
            assert info is not None
            assert locks[info.attrib["DbId"]].findtext("FieldsBlob") in (None, "")
        for index, video in enumerate(tree.iter("Sm2TiVideoClip")):
            assert video.findtext("Name") == values[index].title
            assert video.findtext("MediaFilePath") == str(
                mounts[index].path
                / f"{mounts[index].source.stem}-[000000-{mounts[index].frames - 1:06d}].dng"
            )


@pytest.mark.parametrize("writer", [write_bin, write_timeline])
def test_invalid_saved_details_refuse_the_archive(
    tiny_raw: Path,
    tmp_path: Path,
    writer: Callable[..., object],
) -> None:
    from mcrawsome.resolve.native import send_mounts

    mount = mounted(tiny_raw, tmp_path / "virtual")
    record = write_annotations(tiny_raw, clip_id(tiny_raw), ClipAnnotations(description="Saved"), "")
    document: dict[str, Any] = record.document
    document["schema_version"] = 999
    tiny_raw.with_suffix(".meta").write_text(json.dumps(document))
    target = tmp_path / "refused.zip"
    with pytest.raises(ValueError, match="clip details"):
        writer(target, [mount], name="Refused")
    assert not target.exists()
    result = send_mounts([mount], "auto", mode="timeline" if writer is write_timeline else "bin")
    assert not result.launched
    assert result.archive is None
    assert "clip details" in result.results[0].message


def test_multiple_clips_have_correct_descriptors_and_independent_ids(
    tiny_raw: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mount = mounted(tiny_raw, tmp_path / "virtual")
    monkeypatch.setattr(Clip, "raw_frame", lambda *_: pytest.fail("archive must not decode pixels"))
    paths = [tmp_path / f"{i}.drb" for i in range(2)]
    for path in paths:
        write_bin(path, [mount, mount], name="mcrawsome clips")
    documents = [trees(path) for path in paths]
    for document in documents:
        for tree in document:
            for lock in tree.iter("BtMetadataLockableBlob"):
                if lock.findtext("FieldsBlob"):
                    fields = metadata_fields(lock)
                    assert fields[107] == "36"
                    assert fields[99] == "0/9"
                    assert not ({64, 65, 66, 67, 68, 74, 77, 80, 88} & fields.keys())
    identity_sets = []
    for document in documents:
        ids = [node.attrib["DbId"] for tree in document for node in tree.iter() if "DbId" in node.attrib]
        assert len(ids) == len(set(ids))
        identity_sets.append(set(ids))
        video = [node for tree in document for node in tree.iter("BtVideoInfo")]
        audio = [node for tree in document for node in tree.iter("BtAudioInfo")]
        assert len(video) == len(audio) == 2
        for node in video:
            descriptor = {field.number: field.value for field in decode_wire(raw(node, "Clip"))}
            assert descriptor[1] == str(mount.path).encode()
            assert descriptor[2] == b"tiny-[000000-000002].dng"
            assert descriptor[6] == b"tiny-%06u.dng"
            assert descriptor[13] == mount.stamp[3] // 1000
            time = decode_map(raw(node, "Time"))
            assert time["NumFrames"].value == struct.pack(">i", 3)
            assert time["FrameRate"].value == struct.pack("<dd", 24, 0)
            geometry = decode_map(raw(node, "Geometry"))
            assert geometry["Resolution"].value == struct.pack(">QQ", 128, 64)
            expected_size = (mount.path / "tiny-000000.dng").stat().st_size
            assert int.from_bytes(geometry["FrameSize"].value or b"", "big") == expected_size
        for node in audio:
            track = decode_map(decode_map(raw(node, "TracksBA"))["0"].value or b"")
            assert int.from_bytes(track["Duration"].value or b"", "big") == 6000
            assert int.from_bytes(track["NumChannels"].value or b"", "big") == 2
        pool_ids = {node.attrib["DbId"] for tree in document for node in tree.iter("Sm2MpFolder")}
        assert all(node.text in pool_ids for tree in document for node in tree.iter("MpFolder"))
    assert identity_sets[0].isdisjoint(identity_sets[1])


def test_rejects_changed_source_without_leaving_an_archive(tiny_raw: Path, tmp_path: Path) -> None:
    mount = mounted(tiny_raw, tmp_path / "virtual")
    tiny_raw.touch()
    target = tmp_path / "refused.drb"
    with pytest.raises(ValueError, match=r"changed|offline"):
        write_bin(target, [mount], name="mcrawsome")
    assert not target.exists()


@pytest.mark.parametrize("frames", [1, 3])
def test_capture_snapshot_reads_only_first_frame_and_keeps_measured_source_rate(
    make_mcraw: Callable[..., Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    frames: int,
) -> None:
    source = make_mcraw(tmp_path / "timing.mcraw", [i * 41600000 for i in range(frames)])
    mount = mounted(source, tmp_path / "virtual")
    original = Clip.frame_metadata

    def first_only(clip: Clip, index: int) -> FrameMetadata:
        assert index == 0, "handoff must not scan later capture values"
        return original(clip, index)

    monkeypatch.setattr(Clip, "frame_metadata", first_only)
    media = mounted_media(mount)
    assert media.info.make == "MotionCam"
    assert media.capture is not None
    assert media.capture.manufacturer == "Google"
    assert media.capture.iso == 36
    assert media.rate == 24
    if frames > 1:
        assert media.capture.measured_fps == pytest.approx(1e9 / 41600000)
        assert media.capture.measured_fps != media.rate
    else:
        assert media.capture.measured_fps is None


@pytest.mark.parametrize("channels", [0, 1, 2])
def test_audio_channels_and_references_match_the_wav(tiny_raw: Path, tmp_path: Path, channels: int) -> None:
    from mcrawsome.io.container import ContainerWriter, read_frame_record, read_index

    source = tmp_path / "audio.mcraw"
    with tiny_raw.open("rb") as stream:
        index = read_index(stream)
        index.metadata["extraData"]["audioChannels"] = channels
        with ContainerWriter(source, index.metadata) as writer:
            for frame in index.frames:
                payload, metadata = read_frame_record(stream, frame.offset)
                writer.add_frame(frame.timestamp_ns, payload, metadata)
            if channels:
                writer.add_audio(b"\0" * 6000 * 2 * channels, 0)
    mount = mounted(source, tmp_path / "virtual")
    target = tmp_path / "audio.drb"
    write_bin(target, [mount], name="Audio", separate_audio=True)
    document = trees(target)
    audio = [node for tree in document for node in tree.iter("Sm2MpAudioClip")]
    assert len(audio) == (1 if channels else 0)
    fields = next(
        metadata_fields(lock)
        for tree in document
        for lock in tree.iter("BtMetadataLockableBlob")
        if lock.findtext("FieldsBlob")
    )
    if channels:
        assert fields[220] == "48"
        assert fields[213] == "WAV"
        assert fields[211].startswith(f"{channels} channel")
    else:
        assert not ({211, 213, 220} & fields.keys())
    if audio:
        descriptor = audio[0].find("EmbeddedAudioVec/Element/BtAudioInfo")
        assert descriptor is not None
        media_id = descriptor.attrib["DbId"]
        outer = next(f.value for f in decode_wire(raw(audio[0], "FieldsBlob")) if f.number == 1)
        assert isinstance(outer, bytes)
        tracks = next(f.value for f in decode_wire(outer) if f.number == 5)
        assert isinstance(tracks, bytes)
        track = decode_map(decode_map(tracks)["0"].value or b"")
        assert track["MediaExtents"].value == struct.pack("<dd", 0, 0.125)
        channel_map = decode_map(track["ChannelVecBA"].value or b"")
        assert set(channel_map) == {str(i) for i in range(channels)}
        for number, field in channel_map.items():
            values = decode_map(field.value or b"")
            assert values["MediaRef"].value == media_id.encode("utf-16-be")
            # Resolve's left channel omits the zero-valued ChannelIdx field.
            assert int.from_bytes(values.get("ChannelIdx", MapField(2, 0, b"\0")).value or b"", "big") == int(
                number
            )
    owners = [node.text for tree in document for node in tree.iter("BlobOwner")]
    descriptor_ids = [
        node.attrib["DbId"]
        for tree in document
        for tag in ("BtAudioInfo", "BtVideoInfo", "Sm2MediaPool")
        for node in tree.iter(tag)
    ]
    assert set(owners) == set(descriptor_ids)


def test_existing_archive_and_neighbouring_partial_file_survive(tiny_raw: Path, tmp_path: Path) -> None:
    mount = mounted(tiny_raw, tmp_path / "virtual")
    target = tmp_path / "keep.drb"
    partial = target.with_suffix(".part")
    target.write_bytes(b"original")
    partial.write_bytes(b"other operation")
    with pytest.raises(FileExistsError):
        write_bin(target, [mount], name="Keep")
    assert target.read_bytes() == b"original"
    assert partial.read_bytes() == b"other operation"


@pytest.mark.parametrize("writer", [write_bin, write_timeline])
def test_synced_audio_keeps_its_wav_dependency_and_matches_clip_titles(
    tiny_raw: Path, tmp_path: Path, writer: Callable[..., object]
) -> None:
    mount = mounted(tiny_raw, tmp_path / "media")
    identity = clip_id(tiny_raw)
    title = "Matching Café 00000000-0000-4000-8000-000000000001"
    write_annotations(tiny_raw, identity, ClipAnnotations(title=title), "")
    for linked in (True, False):
        sidecar = tiny_raw.with_suffix(".meta")
        document = json.loads(sidecar.read_text())
        document["details"]["link_audio"] = linked
        sidecar.write_text(json.dumps(document))
        path = tmp_path / f"linked-{linked}.zip"
        writer(path, [mount], name="Audio sync", separate_audio=True)
        document = trees(path)
        video = next(node for tree in document for node in tree.iter("Sm2MpVideoClip"))
        audio = next(node for tree in document for node in tree.iter("Sm2MpAudioClip"))
        assert video.findtext("AudioSource") == "AUDIO_SOURCE_CUSTOM"
        if writer is write_timeline:
            partners = [
                node
                for tree in document
                for tag in ("Sm2TiVideoClip", "Sm2TiAudioClip")
                for node in tree.iter(tag)
            ]
            assert len(partners) == 2
            for node in partners:
                fields = raw(node, "FieldsBlob")
                for _ in range(2):
                    value = next(field.value for field in decode_wire(fields) if field.number == 1)
                    assert isinstance(value, bytes)
                    fields = value
                link = next(field.value for field in decode_wire(fields) if field.number == 1)
                assert isinstance(link, bytes)
                assert decode_map(link)["0"].value == next(
                    other for other in partners if other is not node
                ).attrib["DbId"].encode("utf-16-be")
        assert video.findtext("Name") == audio.findtext("Name") == title
        properties = raw(audio, "FieldsBlob")
        for _ in range(2):
            value = next(field.value for field in decode_wire(properties) if field.number == 1)
            assert isinstance(value, bytes)
            properties = value
        assert next(field.value for field in decode_wire(properties) if field.number == 2) == title.encode()
        descriptor = audio.find("EmbeddedAudioVec/Element/BtAudioInfo")
        assert descriptor is not None
        assert len(list(video.iter("BtAudioInfo"))) == 0
        values = {field.number: field.value for field in decode_wire(raw(descriptor, "Clip"))}
        assert values[2] == b"tiny.wav"
        assert values[1] == str(mount.path).encode()
        properties = raw(video, "FieldsBlob")
        for _ in range(2):
            value = next(field.value for field in decode_wire(properties) if field.number == 1)
            assert isinstance(value, bytes)
            properties = value
        tracks = next(field.value for field in decode_wire(properties) if field.number == 5)
        assert isinstance(tracks, bytes)
        track = decode_map(decode_map(tracks)["0"].value or b"")
        assert track["Origin"].value == struct.pack(">I", 1)
        for channel in decode_map(track["ChannelVecBA"].value or b"").values():
            assert decode_map(channel.value or b"")["MediaRef"].value == descriptor.attrib["DbId"].encode(
                "utf-16-be"
            )


@pytest.mark.parametrize("writer", [write_bin, write_timeline])
def test_embedded_gps_is_appended_to_manual_location_in_bin_and_timeline(
    tiny_raw: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, writer: Callable[..., object]
) -> None:
    from mcrawsome.io.container import ContainerWriter, read_audio_chunk, read_frame_record, read_index

    source = tmp_path / "gps.mcraw"
    with tiny_raw.open("rb") as stream:
        index = read_index(stream)
        index.metadata["extraData"]["postProcessSettings"].update(
            {
                "gpsLatitude": 51.501,
                "gpsLongitude": -0.12,
                "gpsAltitude": -3.5,
                "gpsTime": "2026-10-20T12:00:00Z",
            }
        )
        with ContainerWriter(source, index.metadata) as output:
            for frame in index.frames:
                data, metadata = read_frame_record(stream, frame.offset)
                output.add_frame(frame.timestamp_ns, data, metadata)
            for audio in index.audio:
                timestamp, samples = read_audio_chunk(stream, audio.offset)
                output.add_audio(samples.astype("<i2").tobytes(), timestamp)
    mount = mounted(source, tmp_path / "gps-media")
    write_annotations(source, clip_id(source), ClipAnnotations(location="Stage B"), "")
    before = source.read_bytes()
    monkeypatch.setattr(Clip, "raw_frame", lambda *_: pytest.fail("GPS export must not decode pixels"))
    monkeypatch.setattr(Clip, "audio", lambda *_: pytest.fail("GPS export must not join PCM"))
    path = tmp_path / "gps-archive.zip"
    writer(path, [mount], name="GPS metadata")
    document = trees(path)
    video = next(node for tree in document for node in tree.iter("BtVideoInfo"))
    lock = next(
        node
        for tree in document
        for node in tree.iter("BtMetadataLockableBlob")
        if node.findtext("BlobOwner") == video.attrib["DbId"]
    )
    values = metadata_fields(lock)
    assert values[88] == "Stage B (51.501000, -0.120000)"
    assert "GPS latitude: 51.501 degrees" in values[100]
    assert "GPS longitude: -0.12 degrees" in values[100]
    assert "GPS altitude: -3.5 m (as recorded)" in values[100]
    assert "GPS time: 2026-10-20T12:00:00Z (as recorded)" in values[100]
    assert source.read_bytes() == before


@pytest.mark.parametrize("writer", [write_bin, write_timeline])
@pytest.mark.parametrize(("separate", "linked"), [(False, False), (False, True), (True, False), (True, True)])
def test_audio_layout_keeps_sources_and_links_consistent(
    tiny_raw: Path, tmp_path: Path, writer: Callable[..., object], separate: bool, linked: bool
) -> None:
    mount = mounted(tiny_raw, tmp_path / "media")
    path = tmp_path / "layout.zip"
    writer(path, [mount], name="Audio layout", separate_audio=separate, link_audio=linked)
    document = trees(path)
    video = next(node for tree in document for node in tree.iter("Sm2MpVideoClip"))
    audio = [node for tree in document for node in tree.iter("Sm2MpAudioClip")]
    assert len(audio) == int(separate)
    descriptors = [node for tree in document for node in tree.iter("BtAudioInfo")]
    assert len(descriptors) == 1
    if not separate:
        assert video.find("EmbeddedAudioVec/Element/BtAudioInfo") is descriptors[0]
    assert (video.findtext("AudioSource") == "AUDIO_SOURCE_CUSTOM") is (not separate or linked)
    ids = [node.attrib["DbId"] for tree in document for node in tree.iter() if "DbId" in node.attrib]
    assert len(ids) == len(set(ids))
    owners = {node.text for tree in document for node in tree.iter("BlobOwner")}
    assert descriptors[0].attrib["DbId"] in owners
    if writer is write_timeline:
        clips = [
            node
            for tree in document
            for tag in ("Sm2TiVideoClip", "Sm2TiAudioClip")
            for node in tree.iter(tag)
        ]
        assert len(clips) == 2
        pools = {video.attrib["DbId"], *(node.attrib["DbId"] for node in audio)}
        for node in clips:
            assert node.findtext("MediaRef") in pools
            if not separate:
                assert node.findtext("MediaRef") == video.attrib["DbId"]
            fields = raw(node, "FieldsBlob")
            for _ in range(2):
                nested = next(field.value for field in decode_wire(fields) if field.number == 1)
                assert isinstance(nested, bytes)
                fields = nested
            assert any(field.number == 1 for field in decode_wire(fields)) is (not separate or linked)


@pytest.mark.parametrize(("separate", "linked"), [(False, False), (False, True), (True, False), (True, True)])
def test_bin_audio_layout_preserves_pcm_and_channel_dependencies(
    tiny_raw: Path, tmp_path: Path, separate: bool, linked: bool
) -> None:
    mount = mounted(tiny_raw, tmp_path / "media")
    wav = mount.path / "tiny.wav"
    with Sequence(tiny_raw) as sequence:
        assert sequence.wave is not None
        pcm = sequence.wave.read(0, sequence.wave.size)
        wav.write_bytes(pcm)
    target = tmp_path / "layout.drb"
    write_bin(target, [mount], name="Layout", separate_audio=separate, link_audio=linked)
    document = trees(target)
    videos = [node for tree in document for node in tree.iter("Sm2MpVideoClip")]
    audios = [node for tree in document for node in tree.iter("Sm2MpAudioClip")]
    assert len(videos) == 1
    assert len(audios) == int(separate)
    video = videos[0]
    descriptors = [node for tree in document for node in tree.iter("BtAudioInfo")]
    assert len(descriptors) == 1
    descriptor = descriptors[0]
    values = {field.number: field.value for field in decode_wire(raw(descriptor, "Clip"))}
    assert isinstance(values[1], bytes)
    assert isinstance(values[2], bytes)
    audio_path = Path(values[1].decode()) / values[2].decode()
    assert audio_path.read_bytes() == pcm
    # Every layout reads the mount's own WAV: Resolve keeps it open while the clip is loaded, so the
    # media shows as in use for as long as Resolve has it.
    assert audio_path == wav
    assert not list(tmp_path.glob("*.audio-*.wav"))
    if separate:
        assert not list(video.iter("BtAudioInfo"))
    else:
        assert list(video.iter("BtAudioInfo")) == [descriptor]
    if not separate or linked:
        assert video.findtext("AudioSource") == "AUDIO_SOURCE_CUSTOM"
        properties = raw(video, "FieldsBlob")
        for _ in range(2):
            child = next(field.value for field in decode_wire(properties) if field.number == 1)
            assert isinstance(child, bytes)
            properties = child
        tracks = next(field.value for field in decode_wire(properties) if field.number == 5)
        assert isinstance(tracks, bytes)
        track = decode_map(decode_map(tracks)["0"].value or b"")
        assert track["Origin"].value == struct.pack(">I", 1)
        for channel in decode_map(track["ChannelVecBA"].value or b"").values():
            assert decode_map(channel.value or b"")["MediaRef"].value == descriptor.attrib["DbId"].encode(
                "utf-16-be"
            )
    else:
        assert video.findtext("AudioSource") == "AUDIO_SOURCE_EMBEDDED"
        assert video.findtext("VirtualAudioTracksBA") == ""
    descriptor_ids = {
        node.attrib["DbId"]
        for tree in document
        for tag in ("BtAudioInfo", "BtVideoInfo", "Sm2MediaPool")
        for node in tree.iter(tag)
    }
    assert {node.text for tree in document for node in tree.iter("BlobOwner")} == descriptor_ids


def test_default_bin_is_one_item_that_copies_no_audio(tiny_raw: Path, tmp_path: Path) -> None:
    mount = mounted(tiny_raw, tmp_path / "media")
    target = tmp_path / "one.drb"
    present = set(tmp_path.iterdir())
    write_bin(target, [mount], name="One item")
    document = trees(target)
    assert len([node for tree in document for node in tree.iter("Sm2MpVideoClip")]) == 1
    assert not [node for tree in document for node in tree.iter("Sm2MpAudioClip")]
    assert set(tmp_path.iterdir()) == present | {target}
    original = target.read_bytes()
    with pytest.raises(FileExistsError):
        write_bin(target, [mount], name="Refused")
    assert target.read_bytes() == original
    assert set(tmp_path.iterdir()) == present | {target}


@pytest.mark.parametrize("mode", ["bin", "timeline"])
@pytest.mark.parametrize("studio", [False, True])
def test_edition_changes_only_timeline_size_and_keeps_full_media(
    tiny_raw: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, studio: bool
) -> None:
    from dataclasses import replace

    from mcrawsome.resolve import bin_archive, timeline_archive

    mount = mounted(tiny_raw, tmp_path / "media")
    item = mounted_media(mount)
    full = replace(item, info=replace(item.info, width=4080, height=3072))
    monkeypatch.setattr(bin_archive, "mounted_media", lambda _: full)
    monkeypatch.setattr(timeline_archive, "mounted_media", lambda _: full)
    monkeypatch.setattr(Clip, "raw_frame", lambda *_: pytest.fail("edition sizing decoded RAW"))
    output = tmp_path / ("full.drb" if mode == "bin" else "fitted.drt")
    if mode == "bin":
        write_bin(output, [mount], name="Full media")
    else:
        write_timeline(output, [mount], name="Fitted timeline", full_resolution=studio)
    tree_list = trees(output)
    video = next(node for tree in tree_list for node in tree.iter("BtVideoInfo"))
    assert decode_map(raw(video, "Geometry"))["Resolution"].value == struct.pack(">QQ", 4080, 3072)
    if mode == "timeline":
        sequence = next(node for tree in tree_list for node in tree.iter("Sm2Sequence"))
        setup = decode_map(raw(sequence, "FieldsBlob"))["SequenceSetup"].value
        assert setup is not None
        expected = (4080, 3072) if studio else (2868, 2160)
        assert tuple(struct.unpack_from(">I", setup, i)[0] for i in (44, 52)) == expected


@pytest.mark.parametrize(("separate", "linked"), [(False, False), (False, True), (True, False), (True, True)])
def test_timeline_honours_bin_audio_layout_and_linking(
    tiny_raw: Path, tmp_path: Path, separate: bool, linked: bool
) -> None:
    mount = mounted(tiny_raw, tmp_path / "media")
    wav = mount.path / "tiny.wav"
    with Sequence(tiny_raw) as sequence:
        assert sequence.wave is not None
        pcm = sequence.wave.read(0, sequence.wave.size)
        wav.write_bytes(pcm)
    target = tmp_path / "layout.drt"
    write_timeline(target, [mount], name="Layout", separate_audio=separate, link_audio=linked)
    document = trees(target)
    video = next(node for tree in document for node in tree.iter("Sm2MpVideoClip"))
    audios = [node for tree in document for node in tree.iter("Sm2MpAudioClip")]
    assert len(audios) == int(separate)
    descriptor = next(node for tree in document for node in tree.iter("BtAudioInfo"))
    values = {field.number: field.value for field in decode_wire(raw(descriptor, "Clip"))}
    assert isinstance(values[1], bytes)
    assert isinstance(values[2], bytes)
    audio_path = Path(values[1].decode()) / values[2].decode()
    assert audio_path == wav
    assert audio_path.read_bytes() == pcm
    assert not list(tmp_path.glob("*.audio-*.wav"))
    assert bool(list(video.iter("BtAudioInfo"))) is not separate
    for tree in document:
        for node in tree.iter():
            if node.tag not in ("Sm2TiAudioClip", "Sm2TiVideoClip"):
                continue
            expected = audios[0] if separate and node.tag == "Sm2TiAudioClip" else video
            assert node.findtext("MediaRef") == expected.attrib["DbId"]
            inner = raw(node, "FieldsBlob")
            for _ in range(2):
                child = next(field.value for field in decode_wire(inner) if field.number == 1)
                assert isinstance(child, bytes)
                inner = child
            assert any(field.number == 1 for field in decode_wire(inner)) == (not separate or linked)


def test_failed_combined_timeline_publication_leaves_nothing_behind(
    tiny_raw: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mount = mounted(tiny_raw, tmp_path / "media")
    target = tmp_path / "failed.drt"

    def refused(*args: Any, **kwargs: Any) -> None:
        raise OSError("archive write failed")

    monkeypatch.setattr("mcrawsome.resolve.timeline_archive.ZipFile", refused)
    with pytest.raises(OSError, match="archive write failed"):
        write_timeline(target, [mount], name="Failure")
    assert not target.exists()
    assert not list(tmp_path.glob("*.audio-*.wav"))
    assert not list(tmp_path.glob("*.part"))


@pytest.mark.parametrize("mode", ["bin", "timeline"])
def test_a_time_of_day_mount_gives_resolve_the_clips_start_timecode(
    tiny_raw: Path, tmp_path: Path, mode: str
) -> None:
    """Resolve takes a sent clip's Start TC from its Time record, as its own exports write it (a Timecode
    after UniqueId), not from the DNGs' TimeCodes tags; counting from zero leaves the record as it was."""
    writer = write_bin if mode == "bin" else write_timeline
    timed = mounted(tiny_raw, tmp_path / "timed", "UTC")
    writer(tmp_path / f"timed.{mode}", [timed], name="Timed")
    with Sequence(tiny_raw, timecode="UTC") as sequence:
        assert sequence.timecode is not None
        expected = sequence.timecode.text()
    info = next(node for tree in trees(tmp_path / f"timed.{mode}") for node in tree.iter("BtVideoInfo"))
    fields = decode_map(raw(info, "Time"))
    assert list(fields)[:2] == ["UniqueId", "Timecode"]
    assert fields["Timecode"].kind == 10
    assert fields["Timecode"].value == expected.encode("utf-16-be")
    plain = mounted(tiny_raw, tmp_path / "plain")
    writer(tmp_path / f"plain.{mode}", [plain], name="Plain")
    info = next(node for tree in trees(tmp_path / f"plain.{mode}") for node in tree.iter("BtVideoInfo"))
    assert "Timecode" not in decode_map(raw(info, "Time"))
