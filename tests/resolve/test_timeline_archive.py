"""Native timeline references, pairing and timing, independent of Resolve UI automation."""

import struct
import xml.etree.ElementTree as ET
from dataclasses import replace
from pathlib import Path
from typing import Any
from zipfile import ZipFile

import pytest

from mcrawsome.mount.manager import MountedSequence
from mcrawsome.mount.sequence import source_stamp
from mcrawsome.output.dng import DngInfo
from mcrawsome.resolve import timeline_archive
from mcrawsome.resolve.bin_archive import Media, _identifiers
from mcrawsome.resolve.bin_codec import decode_map, decode_wire, unwrap
from mcrawsome.resolve.timeline_archive import _write as build
from mcrawsome.resolve.timeline_archive import write_timeline


@pytest.fixture
def media(tmp_path: Path) -> list[Media]:
    identity = (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0)
    info = DngInfo(
        256,
        144,
        "rggb",
        (0, 0, 0, 0),
        65535,
        identity,
        identity,
        None,
        None,
        None,
        None,
        17,
        21,
        None,
        (0.95, 1.0, 1.08),
        1,
        "Synthetic",
        "Timeline probe",
        None,
        0.0,
        b"",
    )
    return [
        Media(tmp_path, "probe_a", 24, 24.0, info, 75000, 1700000000000000000, 48000, 48000, 2),
        Media(tmp_path, "probe_b", 48, 24.0, info, 75000, 1700000000000000000, 96000, 48000, 2),
    ]


def documents(path: Path) -> list[ET.Element]:
    with ZipFile(path) as archive:
        assert archive.testzip() is None
        assert len(archive.namelist()) == 3
        trees = [
            ET.fromstring(archive.read(n).decode().replace("ListMgt::", "ListMgt__"))
            for n in archive.namelist()
        ]
        assert f"SeqContainer/{trees[2].attrib['DbId']}.xml" in archive.namelist()
        return trees


def payload(node: ET.Element, tag: str) -> bytes:
    return unwrap(bytes.fromhex(node.findtext(tag) or ""))[0]


def section(raw: bytes, number: int) -> bytes:
    value = next(f.value for f in decode_wire(raw) if f.number == number)
    assert isinstance(value, bytes)
    return value


def test_native_graph_timing_and_links(tmp_path: Path, media: list[Media]) -> None:
    path = tmp_path / "paired.drt"
    build(path, media, name="Pairs probe", linked=True)
    project, master, container = documents(path)
    sequence = master.find(".//Sm2Sequence")
    timeline = master.find(".//Sm2Timeline")
    assert sequence is not None
    assert timeline is not None
    assert project.findtext("TimelineHandleVec/Element") == timeline.attrib["DbId"]
    assert sequence.findtext("Parent") == timeline.attrib["DbId"]
    fields = decode_map(payload(sequence, "FieldsBlob"))
    assert fields["SeqRef"].value == container.attrib["DbId"].encode("utf-16-be")
    assert "Thumbnail" not in fields
    assert payload(sequence, "MediaExtents") == struct.pack("<dd", 3600, 3)
    setup = fields["SequenceSetup"].value
    assert setup is not None
    assert [struct.unpack_from(">I", setup, i)[0] for i in (44, 52, 80, 232, 240)] == [256, 144, 24, 256, 144]
    videos = list(container.iter("Sm2TiVideoClip"))
    audios = list(container.iter("Sm2TiAudioClip"))
    assert len(videos) == len(audios) == 2
    pool_ids = {n.attrib["DbId"] for n in master.iter() if n.tag in ("Sm2MpVideoClip", "Sm2MpAudioClip")}
    for i, (video, audio) in enumerate(zip(videos, audios, strict=True)):
        for node, partner in ((video, audio), (audio, video)):
            assert int(node.findtext("Start") or "0") == 86400 + i * 24
            assert int(node.findtext("Duration") or "0") == media[i].frames
            assert node.findtext("MediaRef") in pool_ids
            inner = section(section(payload(node, "FieldsBlob"), 1), 1)
            assert sum(f.number == 1 for f in decode_wire(inner)) == 1
            assert decode_map(section(inner, 1))["0"].value == partner.attrib["DbId"].encode("utf-16-be")
            assert next(f.value for f in decode_wire(inner) if f.number == 21) == 86400 + i * 24
            assert payload(node, "MediaTimemapBA") == b"\x02" + struct.pack(">d", (media[i].frames - 1) / 24)
    ids = [
        n.attrib["DbId"] for tree in (project, master, container) for n in tree.iter() if "DbId" in n.attrib
    ]
    assert len(ids) == len(set(ids))
    for track in container.iter("Sm2TiTrack"):
        assert track.findtext("Sequence") == sequence.attrib["DbId"]
    owners = {n.text for n in project.iter("BlobOwner")}
    expected = {
        n.attrib["DbId"]
        for tree in (project, master)
        for n in tree.iter()
        if n.tag in ("BtVideoInfo", "BtAudioInfo", "Sm2MediaPool")
    }
    assert owners == expected


def test_fresh_ids_and_unlinked_control(tmp_path: Path, media: list[Media]) -> None:
    paths = [tmp_path / f"{i}.drt" for i in range(2)]
    for path in paths:
        build(path, media, name="Control", linked=False)
    assert _identifiers(documents(paths[0])).isdisjoint(_identifiers(documents(paths[1])))
    for node in documents(paths[0])[2].iter():
        if node.tag in ("Sm2TiVideoClip", "Sm2TiAudioClip"):
            inner = section(section(payload(node, "FieldsBlob"), 1), 1)
            assert all(f.number != 1 for f in decode_wire(inner))


def test_silent_clip_has_no_audio_or_link(tmp_path: Path, media: list[Media]) -> None:
    path = tmp_path / "silent.drt"
    build(
        path, [replace(media[0], channels=0, samples=0, sample_rate=0), media[1]], name="Silent", linked=True
    )
    _, master, container = documents(path)
    assert len(list(master.iter("Sm2MpAudioClip"))) == 1
    assert len(list(container.iter("Sm2TiAudioClip"))) == 1
    first = next(container.iter("Sm2TiVideoClip"))
    inner = section(section(payload(first, "FieldsBlob"), 1), 1)
    assert all(f.number != 1 for f in decode_wire(inner))


def test_mono_virtual_channel_map(tmp_path: Path, media: list[Media]) -> None:
    path = tmp_path / "mono.drt"
    build(path, [replace(media[0], channels=1)], name="Mono probe", linked=True)
    _, master, container = documents(path)
    audio = next(container.iter("Sm2TiAudioClip"))
    fields = decode_map(payload(audio, "VirtualAudioTrackBA"))
    assert fields["AudioType"].value == struct.pack(">i", 1)
    assert fields["ChannelsBA"].value == struct.pack(">III", 2, 1, 0x4001)
    info = next(master.iter("BtAudioInfo"))
    track = decode_map(decode_map(payload(info, "TracksBA"))["0"].value or b"")
    assert track["NumChannels"].value == struct.pack(">i", 1)


@pytest.mark.parametrize(
    ("change", "says"),
    [
        ({"rate": 25.0}, "need every clip at one frame rate: probe_a is 24 fps and probe_b is 25 fps"),
        ({"channels": 3}, "probe_b has 3 audio channels; timelines take mono or stereo audio"),
        ({"frames": 0}, "probe_b has no frames to place on a timeline"),
    ],
)
def test_unverified_or_invalid_media_refused_naming_the_clip_without_output(
    tmp_path: Path, media: list[Media], change: dict[str, Any], says: str
) -> None:
    path = tmp_path / "refused.drt"
    with pytest.raises(ValueError, match="probe_b") as caught:
        build(path, [media[0], replace(media[1], **change)], name="Invalid", linked=True)
    assert says in str(caught.value)
    assert not path.exists()


def test_a_later_clip_at_a_rate_that_is_not_whole_is_named_with_its_rate_and_the_fix(
    tmp_path: Path, media: list[Media]
) -> None:
    """Every clip is checked, not only the first: a 23.79 fps recording shows as 24 on its card."""
    with pytest.raises(ValueError, match=r"probe_b records 23\.792 fps") as caught:
        build(tmp_path / "odd.drt", [media[0], replace(media[1], rate=23.7916)], name="Odd", linked=True)
    message = str(caught.value)
    assert "probe_a" not in message
    assert "Mounted frame rate to Nearest whole rate" in message
    assert not (tmp_path / "odd.drt").exists()


def test_several_clips_at_rates_that_are_not_whole_are_all_named(tmp_path: Path, media: list[Media]) -> None:
    clips = [replace(media[0], rate=23.976), replace(media[1], rate=29.97)]
    with pytest.raises(ValueError, match=r"probe_a \(23\.976 fps\) and probe_b \(29\.97 fps\)") as caught:
        build(tmp_path / "odd.drt", clips, name="Odd", linked=True)
    assert "Apply to Selected" in str(caught.value)


def test_does_not_replace_existing_file(tmp_path: Path, media: list[Media]) -> None:
    path = tmp_path / "existing.drt"
    path.write_bytes(b"keep")
    with pytest.raises(FileExistsError):
        build(path, media, name="Existing", linked=True)
    assert path.read_bytes() == b"keep"


@pytest.mark.parametrize("rate", [25.0, 30.0, 50.0, 60.0])
def test_whole_number_rate_changes_sequence_and_start(
    tmp_path: Path, media: list[Media], rate: float
) -> None:
    path = tmp_path / "rate.drt"
    build(path, [replace(media[0], rate=rate)], name="Rate", linked=True)
    _, master, container = documents(path)
    sequence = master.find(".//Sm2Sequence")
    assert sequence is not None
    assert payload(sequence, "FrameRate") == struct.pack("<dd", rate, 0)
    for tag in ("Sm2TiVideoClip", "Sm2TiAudioClip"):
        item = next(container.iter(tag))
        assert int(item.findtext("Start") or "0") == round(rate * 3600)
        assert payload(item, "MediaFrameRate") == struct.pack("<dd", rate, 0)


def test_source_change_during_assembly_leaves_no_archive(
    tmp_path: Path, media: list[Media], monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.mcraw"
    source.write_bytes(b"snapshot")
    mount = MountedSequence(
        id="probe",
        source=source,
        path=tmp_path,
        stamp=source_stamp(source),
        frames=24,
        active=True,
        serving=True,
    )
    monkeypatch.setattr(timeline_archive, "mounted_media", lambda _: media[0])
    original = timeline_archive.configure

    def changed(
        sequence: ET.Element,
        pool: ET.Element,
        items: list[Media],
        rotations: list[int] | None = None,
        *,
        full_resolution: bool = True,
    ) -> None:
        original(sequence, pool, items, rotations, full_resolution=full_resolution)
        source.write_bytes(b"changed source")

    monkeypatch.setattr(timeline_archive, "configure", changed)
    path = tmp_path / "changed.drt"
    with pytest.raises(ValueError, match="Source changed"):
        write_timeline(path, [mount], name="Changed")
    assert not path.exists()
    assert not list(tmp_path.glob("*.part"))


@pytest.mark.parametrize("renderer", ["perspective", "camera"])
def test_gyro_versions_preserve_pairing_raw_defaults_and_frame_timing(
    tmp_path: Path, media: list[Media], renderer: str
) -> None:
    import numpy as np

    from mcrawsome.resolve.camera_gyro import CameraGyroCorrections
    from mcrawsome.resolve.stabilisation import PerspectiveCorrections

    path = tmp_path / "gyro.drt"
    corrections: list[PerspectiveCorrections | CameraGyroCorrections] = [
        PerspectiveCorrections(
            np.tile(np.eye(3, dtype=np.float32), (m.frames, 1, 1)),
            1.15,
            m.info.width,
            m.info.height,
            m.info.orientation,
        )
        for m in media
    ]
    if renderer == "camera":
        corrections = [
            CameraGyroCorrections(
                np.tile(np.array([0, 0, 0, 1], np.float32), (m.frames, 1)),
                (np.arange(m.frames) / m.rate * 1e9).astype(np.float32),
                200.0,
                1.15,
                m.info.width,
                m.info.height,
                m.info.orientation,
            )
            for m in media
        ]
    build(path, media, name="Gyro", corrections=corrections, rotations=[90, 270])
    project, master, container = documents(path)
    gyro_control = tmp_path / "gyro-unrotated.drt"
    build(gyro_control, media, name="Gyro control", corrections=corrections)
    unrotated = documents(gyro_control)[2]
    for rotated_version, original_version in zip(
        container.iter("ListMgt__LmVersion"), unrotated.iter("ListMgt__LmVersion"), strict=True
    ):
        rotated_raw, original_raw = payload(rotated_version, "Body"), payload(original_version, "Body")
        for number in (3, 9, 1, 6, 2):
            rotated_raw, original_raw = section(rotated_raw, number), section(original_raw, number)
        if renderer == "camera":
            assert rotated_raw == original_raw
    assert all(not v.findtext("EffectFiltersBA") for v in unrotated.iter("Sm2TiVideoClip"))
    videos = list(container.iter("Sm2TiVideoClip"))
    audios = list(container.iter("Sm2TiAudioClip"))
    versions = []
    for video, audio, m in zip(videos, audios, media, strict=True):
        assert not video.findtext("EffectFiltersBA")
        assert not audio.findtext("EffectFiltersBA")
        assert video.findtext("Duration") == str(m.frames)
        assert video.findtext("MediaStartTime") == "0"
        assert video.findtext("Start") == audio.findtext("Start")
        assert payload(video, "MediaTimemapBA") == b"\x02" + struct.pack(">d", (m.frames - 1) / m.rate)
        assert audio.find("pLmVerTable") is None
        table = video.find("pLmVerTable/ListMgt__LmVersionTable")
        assert table is not None
        version = table.find("Locals/Element/ListMgt__LmVersion")
        assert version is not None
        assert table.findtext("pActive") == version.attrib["DbId"]
        versions.append(version.attrib["DbId"])
        raw = payload(version, "Body")
        geometry = decode_wire(section(section(raw, 1), 3))
        dims = {f.number: f.value for f in geometry if f.kind == 0}
        assert [dims[k] for k in (1, 2, 4, 5, 7, 8)] == [144, 256, 256, 144, 144, 256]
        for number in (3, 9, 1, 6, 2):
            raw = section(raw, number)
        assert len(decode_wire(section(raw, 4))) == m.frames
        inner = section(section(payload(video, "FieldsBlob"), 1), 1)
        assert decode_map(section(inner, 1))["0"].value == audio.attrib["DbId"].encode("utf-16-be")
    assert len(set(versions)) == 2
    ids = [n.attrib["DbId"] for t in (project, master, container) for n in t.iter() if "DbId" in n.attrib]
    assert len(set(ids)) == len(ids)
    # Video pool descriptor bytes (RAW defaults, identity, geometry) follow the same shared writer.
    control = tmp_path / "control.drt"
    build(control, media, name="Control")
    # Test the measured WB payload directly, independent of randomised object identifiers.
    for actual, expected in zip(
        master.iter("BtVideoInfo"), documents(control)[1].iter("BtVideoInfo"), strict=True
    ):
        from mcrawsome.resolve.rotation import clip_rotation

        turn = 90 if actual is next(master.iter("BtVideoInfo")) else 270
        assert payload(actual, "Radiometry") == clip_rotation(payload(expected, "Radiometry"), turn)
        for tag in ("Time",):
            fields = decode_map(payload(actual, tag))
            reference = decode_map(payload(expected, tag))
            assert {k: v for k, v in fields.items() if k != "UniqueId"} == {
                k: v for k, v in reference.items() if k != "UniqueId"
            }


@pytest.mark.parametrize("rotations", [[90], [90, 45], [90, 270, 180]])
def test_invalid_rotation_list_publishes_no_archive(
    tmp_path: Path, media: list[Media], rotations: list[int]
) -> None:
    path = tmp_path / "invalid.drt"
    with pytest.raises(ValueError, match="rotation"):
        build(path, media, name="Invalid", rotations=rotations)
    assert not path.exists()


def test_display_rotation_cannot_be_applied_to_rotated_media(tmp_path: Path) -> None:
    from mcrawsome.recipe.raw import RawCalibration

    mount = MountedSequence(
        id="test",
        source=tmp_path / "source.mcraw",
        path=tmp_path,
        stamp=(1, 2, 3, 4),
        raw_settings=RawCalibration(rotation=90),
    )
    with pytest.raises(ValueError, match="recorded orientation"):
        write_timeline(tmp_path / "double.drt", [mount], name="Double", rotations=[90])
    assert not (tmp_path / "double.drt").exists()


@pytest.mark.parametrize("turn", [90, 270])
def test_quarter_turn_uses_native_portrait_format_and_clip_properties(
    tmp_path: Path, media: list[Media], turn: int
) -> None:
    source = replace(media[0], info=replace(media[0].info, width=3840, height=2160))
    path = tmp_path / "portrait.drt"
    build(path, [source], name="Portrait", rotations=[turn])
    _, master, container = documents(path)
    sequence = master.find(".//Sm2Sequence")
    assert sequence is not None
    assert payload(sequence, "Resolution") == struct.pack(">QQ", 2160, 3840)
    setup = decode_map(payload(sequence, "FieldsBlob"))["SequenceSetup"].value
    assert setup is not None
    assert len(setup) == 371
    assert [struct.unpack_from(">I", setup, i)[0] for i in (44, 52, 232, 240)] == [2160, 3840] * 2
    assert struct.unpack_from(">QQ", setup, 100) == (1, 1)
    fields = {f.number: f.value for f in decode_wire(setup[344:])}
    assert fields[17] == 68
    assert fields[44] == 14
    video = container.find(".//Sm2TiVideoClip")
    assert video is not None
    assert not video.findtext("EffectFiltersBA")
    radiometry = decode_wire(payload(next(master.iter("BtVideoInfo")), "Radiometry"))
    assert next(f.value for f in radiometry if f.number == 19) == 2 * turn
    assert video.findtext("Duration") == str(source.frames)
    assert video.findtext("MediaStartTime") == "0"
    geometry = decode_map(payload(next(master.iter("BtVideoInfo")), "Geometry"))
    assert geometry["Resolution"].value == struct.pack(">QQ", 3840, 2160)


@pytest.mark.parametrize(
    ("size", "timeline"),
    [
        ((3840, 2160), (3840, 2160)),
        ((4096, 2304), (4096, 2304)),
        ((4080, 3060), (4080, 3060)),
        ((1920, 1080), (1920, 1080)),
    ],
)
def test_timelines_match_source_resolution_without_silent_downscaling(
    media: list[Media], size: tuple[int, int], timeline: tuple[int, int]
) -> None:
    """The timeline uses the original dimensions, including above-UHD sources."""
    clip = replace(media[0], info=replace(media[0].info, width=size[0], height=size[1]))
    assert timeline_archive.timeline_size([clip]) == timeline
    width, height = timeline
    assert size[0] * height == size[1] * width  # gyro timelines need the media's exact shape


def test_a_rate_that_is_not_whole_names_the_clip_and_the_setting_that_fixes_it(
    tmp_path: Path, media: list[Media]
) -> None:
    with pytest.raises(ValueError, match="whole number of frames") as caught:
        build(tmp_path / "odd.drt", [replace(media[0], rate=30.30484)], name="Odd", linked=True)
    message = str(caught.value)
    assert "probe_a records 30.305 fps" in message
    assert "Mounted frame rate" in message
    assert "Nearest whole rate" in message
    assert not (tmp_path / "odd.drt").exists()


@pytest.mark.parametrize(
    ("size", "expected"),
    [
        ((4080, 3072), (2868, 2160)),
        ((4096, 2304), (3840, 2160)),
        ((2160, 3840), (1216, 2160)),
        ((1920, 1080), (1920, 1080)),
        ((7680, 1920), (3840, 960)),
        ((4079, 3071), (2868, 2160)),
    ],
)
def test_free_resolution_uses_the_limiting_edge_with_even_pixel_rounding(
    size: tuple[int, int], expected: tuple[int, int]
) -> None:
    assert timeline_archive.fitted_timeline_size(size, False) == expected
    assert timeline_archive.fitted_timeline_size(size, True) == size
    from mcrawsome.resolve.geometry import matching_aspect

    assert matching_aspect(size, expected)
    if size[0] > 3840 or size[1] > 2160:
        assert expected[0] == 3840 or expected[1] == 2160


def test_free_timeline_changes_timeline_geometry_but_retains_full_media(
    tmp_path: Path, media: list[Media]
) -> None:
    source = replace(media[0], info=replace(media[0].info, width=4080, height=3072))
    path = tmp_path / "free.drt"
    build(path, [source], name="Free", full_resolution=False)
    _, master, _ = documents(path)
    sequence = master.find(".//Sm2Sequence")
    assert sequence is not None
    setup = decode_map(payload(sequence, "FieldsBlob"))["SequenceSetup"].value
    assert setup is not None
    assert [struct.unpack_from(">I", setup, i)[0] for i in (44, 52, 232, 240)] == [2868, 2160] * 2
    geometry = decode_map(payload(next(master.iter("BtVideoInfo")), "Geometry"))
    assert geometry["Resolution"].value == struct.pack(">QQ", 4080, 3072)


@pytest.mark.parametrize("turn", [0, 90])
def test_free_timeline_preserves_camera_gyro_corrections_at_a_reduced_size(
    tmp_path: Path, media: list[Media], turn: int
) -> None:
    import numpy as np

    from mcrawsome.resolve.camera_gyro import CameraGyroCorrections

    source = replace(media[0], info=replace(media[0].info, width=4080, height=3072))
    correction = CameraGyroCorrections(
        np.tile(np.array([0, 0, 0, 1], np.float32), (source.frames, 1)),
        (np.arange(source.frames) / source.rate * 1e9).astype(np.float32),
        200.0,
        1.15,
        4080,
        3072,
        source.info.orientation,
    )
    path = tmp_path / "scaled-gyro.drt"
    build(
        path, [source], name="Scaled gyro", corrections=[correction], rotations=[turn], full_resolution=False
    )
    _, master, container = documents(path)
    sequence = master.find(".//Sm2Sequence")
    assert sequence is not None
    setup = decode_map(payload(sequence, "FieldsBlob"))["SequenceSetup"].value
    assert setup is not None
    expected = timeline_archive.fitted_timeline_size((3072, 4080) if turn else (4080, 3072), False)
    assert tuple(struct.unpack_from(">I", setup, i)[0] for i in (44, 52)) == expected
    version = next(container.iter("ListMgt__LmVersion"))
    geometry = {f.number: f.value for f in decode_wire(section(section(payload(version, "Body"), 1), 3))}
    assert (geometry[1], geometry[2]) == expected
    assert (geometry[4], geometry[5]) == (4080, 3072)


def test_media_start_is_the_start_timecode_in_seconds_as_resolve_writes_it() -> None:
    # Resolve's own export of a Blackmagic RAW clip at 11:29:54:19, 23.976 fps, wrote 41436.18645833333.
    assert timeline_archive.media_start("11:29:54:19", 24000 / 1001) == "41436.18645833333"
    assert timeline_archive.media_start("02:08:19:10", 24.0) == "7699.416666666667"
    assert timeline_archive.media_start("01:00:00:00", 25.0) == "3600"
    assert timeline_archive.media_start("", 24.0) == "0"


@pytest.mark.parametrize("separate_audio", [True, False])
def test_items_start_where_their_media_pool_clip_starts(
    tmp_path: Path, media: list[Media], separate_audio: bool
) -> None:
    """Resolve reads an item from MediaStartTime on its clip's own clock. A clip whose Time record has a
    Start TC begins there, so an item left at 0 reads frames from before the clip (Resolve logs "Read
    outside clip bounds"). A separate WAV clip has no Time record and counts from zero."""
    import wave

    timed = [replace(media[0], timecode="02:08:19:10"), media[1]]
    for item in timed:
        with wave.open(str(tmp_path / f"{item.stem}.wav"), "wb") as output:
            output.setnchannels(item.channels)
            output.setsampwidth(2)
            output.setframerate(item.sample_rate)
            output.writeframes(bytes(item.samples * item.channels * 2))
    path = tmp_path / "timecode.drt"
    build(path, timed, name="Timecode", separate_audio=separate_audio)
    container = documents(path)[2]
    starts = ["7699.416666666667", "0"]
    assert [v.findtext("MediaStartTime") for v in container.iter("Sm2TiVideoClip")] == starts
    audio = [a.findtext("MediaStartTime") for a in container.iter("Sm2TiAudioClip")]
    assert audio == (["0", "0"] if separate_audio else starts)


@pytest.mark.parametrize("channels", [1, 2])
def test_combined_audio_uses_video_source_and_synced_channel_numbers(
    tmp_path: Path, media: list[Media], channels: int
) -> None:
    import wave

    import numpy as np

    item = replace(media[0], channels=channels)
    pcm = np.tile((np.sin(np.arange(48000) * 0.07) * 2000).astype("<i2")[:, None], (1, channels))
    with wave.open(str(tmp_path / f"{item.stem}.wav"), "wb") as output:
        output.setnchannels(channels)
        output.setsampwidth(2)
        output.setframerate(48000)
        output.writeframes(pcm.tobytes())
    target = tmp_path / "combined.drt"
    build(target, [item], name="Combined audio", separate_audio=False)
    _, master, container = documents(target)
    video = next(master.iter("Sm2MpVideoClip"))
    audio = next(container.iter("Sm2TiAudioClip"))
    assert audio.findtext("MediaRef") == video.attrib["DbId"]
    assert audio.findtext("MediaFilePath") == str(item.folder / item.video_name)
    track = decode_map(payload(audio, "VirtualAudioTrackBA"))
    source = decode_map(decode_map(payload(video, "VirtualAudioTracksBA"))["0"].value or b"")
    assert track["ChannelsBA"].value == source["ChannelsBA"].value
    assert track["ChannelsBA"].value == struct.pack(">II", 2, channels) + b"".join(
        struct.pack(">I", 0x20004001 + channel) for channel in range(channels)
    )
