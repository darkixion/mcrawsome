import io
import json
import struct
from pathlib import Path

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from mcrawsome.io import container
from mcrawsome.io.container import (
    ContainerError,
    ContainerWriter,
    read_audio_chunk,
    read_frame_record,
    read_index,
)


def _write_sample(path: Path, n_frames: int = 3, with_audio: bool = True) -> None:
    with ContainerWriter(path, {"whiteLevel": 4095.0, "extraData": {"audioChannels": 2}}) as w:
        for i in range(n_frames):
            ts = 1_000_000_000 + i * 41_666_666
            w.add_frame(ts, bytes([i]) * 10, json.dumps({"timestamp": str(ts)}).encode())
            if with_audio:
                w.add_audio(np.arange(8, dtype=np.int16).tobytes(), ts)


def test_round_trip_frames_and_metadata(tmp_path: Path) -> None:
    p = tmp_path / "a.mcraw"
    _write_sample(p)
    with p.open("rb") as f:
        idx = read_index(f)
        assert idx.metadata["whiteLevel"] == 4095.0
        assert [fr.timestamp_ns for fr in idx.frames] == [1_000_000_000 + i * 41_666_666 for i in range(3)]
        payload, meta = read_frame_record(f, idx.frames[2].offset)
        assert payload == bytes([2]) * 10
        assert json.loads(meta)["timestamp"] == str(1_000_000_000 + 2 * 41_666_666)


def test_round_trip_audio(tmp_path: Path) -> None:
    p = tmp_path / "a.mcraw"
    _write_sample(p)
    with p.open("rb") as f:
        idx = read_index(f)
        assert len(idx.audio) == 3
        ts, samples = read_audio_chunk(f, idx.audio[1].offset)
        assert ts == 1_000_000_000 + 41_666_666
        np.testing.assert_array_equal(samples, np.arange(8, dtype=np.int16))


def test_no_audio_is_allowed(tmp_path: Path) -> None:
    p = tmp_path / "a.mcraw"
    _write_sample(p, with_audio=False)
    with p.open("rb") as f:
        assert read_index(f).audio == ()


def test_rejects_wrong_header() -> None:
    with pytest.raises(ContainerError, match="not a MotionCam"):
        read_index(io.BytesIO(b"NOTMCRAW" + b"\x00" * 64))


def test_recovers_corrupt_index(tmp_path: Path) -> None:
    p = tmp_path / "a.mcraw"
    _write_sample(p)
    data = bytearray(p.read_bytes())
    data[-16] ^= 0xFF  # damage the first byte of the index magic number
    idx = read_index(io.BytesIO(bytes(data)))
    assert idx.recovered
    assert len(idx.frames) == 3
    assert len(idx.audio) == 3


def _start(data: bytes | bytearray) -> int:
    return 16 + int(struct.unpack_from("<II", data, 8)[1])


def _index_start(data: bytes | bytearray) -> int:
    offset = _start(data)
    while offset + 8 <= len(data):
        kind, size = struct.unpack_from("<II", data, offset)
        if kind in (container.ItemType.AUDIO_INDEX, container.ItemType.BUFFER_INDEX_DATA):
            return offset
        offset += 8 + size
    raise AssertionError("test container has no index")


def test_recovers_every_complete_frame_and_audio_chunk_without_an_index(tmp_path: Path) -> None:
    path = tmp_path / "cut.mcraw"
    _write_sample(path)
    data = path.read_bytes()
    expected = read_index(io.BytesIO(data))
    cut = data[: _index_start(data)]
    found = read_index(io.BytesIO(cut))
    assert found.recovered
    assert found.frames == expected.frames
    assert found.audio == expected.audio
    assert container.scan_items(io.BytesIO(cut), _start(cut), len(cut)) == (
        list(expected.frames),
        list(expected.audio),
    )


@pytest.mark.parametrize("cut", [4, 12, 20, 25, 30])
def test_a_frame_cut_in_its_header_payload_or_metadata_is_dropped(tmp_path: Path, cut: int) -> None:
    path = tmp_path / "cut.mcraw"
    _write_sample(path)
    data = path.read_bytes()
    expected = read_index(io.BytesIO(data))
    incomplete = data[: expected.frames[-1].offset + cut]
    found = read_index(io.BytesIO(incomplete))
    assert found.recovered
    assert found.frames == expected.frames[:-1]
    assert found.audio == expected.audio[:-1]


@settings(max_examples=30)
@given(n_frames=st.integers(0, 12), n_audio=st.integers(0, 12))
def test_scanning_matches_the_indexes(
    n_frames: int, n_audio: int, tmp_path_factory: pytest.TempPathFactory
) -> None:
    path = tmp_path_factory.mktemp("scan") / "complete.mcraw"
    with ContainerWriter(path, {}) as writer:
        for i in range(max(n_frames, n_audio)):
            timestamp = (20 - i) * 40_000_000
            if i < n_frames:
                writer.add_frame(timestamp, b"image", json.dumps({"timestamp": str(timestamp)}).encode())
            if i < n_audio:
                writer.add_audio(b"\x00\x00", timestamp if i % 2 else None)
    data = path.read_bytes()
    expected = read_index(io.BytesIO(data))
    assert not expected.recovered
    frames, audio = container.scan_items(io.BytesIO(data), _start(data), len(data))
    assert frames == list(expected.frames)
    assert audio == list(expected.audio)


@pytest.mark.parametrize("junk", [b"junk", struct.pack("<II", 99, 0), struct.pack("<II", 2, 1000)])
def test_junk_after_valid_items_stops_the_scan(tmp_path: Path, junk: bytes) -> None:
    path = tmp_path / "junk.mcraw"
    _write_sample(path)
    data = path.read_bytes()
    expected = read_index(io.BytesIO(data))
    data = data[: _index_start(data)] + junk
    found = read_index(io.BytesIO(data))
    assert found.recovered
    assert found.frames == expected.frames
    assert found.audio == expected.audio


@pytest.mark.parametrize(
    "damage", ["count", "short-count", "index-offset", "frame-offset", "index-type", "audio-count"]
)
def test_other_corrupt_indexes_fall_back_to_scanning(tmp_path: Path, damage: str) -> None:
    path = tmp_path / "bad-index.mcraw"
    _write_sample(path)
    data = bytearray(path.read_bytes())
    expected = read_index(io.BytesIO(data))
    _, _, offset = struct.unpack_from("<iiq", data, len(data) - 16)
    if damage == "count":
        struct.pack_into("<i", data, len(data) - 12, 100_000)
    elif damage == "short-count":
        struct.pack_into("<i", data, len(data) - 12, 1)
    elif damage == "index-offset":
        struct.pack_into("<q", data, len(data) - 8, len(data) + 10)
    elif damage == "frame-offset":
        struct.pack_into("<q", data, offset, -1)
    elif damage == "audio-count":
        struct.pack_into("<q", data, _index_start(data) + 8, 100_000)
    else:
        struct.pack_into("<I", data, len(data) - 24, int(container.ItemType.METADATA))
    found = read_index(io.BytesIO(data))
    assert found.recovered
    assert found.frames == expected.frames
    assert found.audio == expected.audio


@pytest.mark.parametrize("kept", [0, 4])
def test_a_complete_audio_chunk_with_cut_optional_metadata_is_still_readable(
    tmp_path: Path, kept: int
) -> None:
    path = tmp_path / "audio.mcraw"
    _write_sample(path, n_frames=1)
    data = path.read_bytes()
    expected = read_index(io.BytesIO(data))
    offset = expected.audio[0].offset
    cut = data[: offset + 8 + 16 + 8 + kept]
    found = read_index(io.BytesIO(cut))
    assert found.recovered
    assert len(found.audio) == 1
    assert found.audio[0].timestamp_ns == -1
    timestamp, samples = read_audio_chunk(io.BytesIO(cut), found.audio[0].offset)
    assert timestamp is None
    np.testing.assert_array_equal(samples, np.arange(8, dtype=np.int16))


def test_scan_seeks_over_image_and_audio_payloads(tmp_path: Path) -> None:
    path = tmp_path / "large.mcraw"
    with ContainerWriter(path, {}) as writer:
        writer.add_frame(123, b"x" * 65_536, b'{"timestamp": "123"}')
        writer.add_audio(b"x" * 65_536, 123)
    data = path.read_bytes()
    expected = read_index(io.BytesIO(data))

    class HeadersOnly(io.BytesIO):
        def read(self, size: int | None = -1) -> bytes:
            assert size is not None
            assert 0 <= size <= 64, "scan read an image or audio payload"
            return super().read(size)

    frames, audio = container.scan_items(HeadersOnly(data), _start(data), len(data))
    assert frames == list(expected.frames)
    assert audio == list(expected.audio)


@pytest.mark.parametrize("metadata", [b"{", b"{}", b'{"timestamp": "bad"}'])
def test_scan_drops_a_frame_with_unreadable_timestamp(tmp_path: Path, metadata: bytes) -> None:
    path = tmp_path / "metadata.mcraw"
    with ContainerWriter(path, {}) as writer:
        writer.add_frame(123, b"frame", b'{"timestamp": "123"}')
        writer.add_frame(456, b"frame", metadata)
    data = path.read_bytes()
    frames, _ = container.scan_items(io.BytesIO(data), _start(data), len(data))
    assert [frame.timestamp_ns for frame in frames] == [123]


@pytest.mark.parametrize("layout", ["bare-frame-index", "audio-index-header-only"])
def test_reads_motioncam_index_layouts_without_recovery(tmp_path: Path, layout: str) -> None:
    path = tmp_path / "motioncam.mcraw"
    _write_sample(path)
    data = bytearray(path.read_bytes())
    expected = read_index(io.BytesIO(data))
    if layout == "bare-frame-index":
        (offset,) = struct.unpack_from("<q", data, len(data) - 8)
        del data[offset - 8 : offset]
        struct.pack_into("<q", data, len(data) - 8, offset - 8)
    else:
        struct.pack_into("<I", data, _index_start(data) + 4, 16)
    found = read_index(io.BytesIO(data))
    assert not found.recovered
    assert found.frames == expected.frames
    assert found.audio == expected.audio


def _hostile_container(tmp_path: Path, frame_metadata: bytes) -> bytes:
    path = tmp_path / "hostile.mcraw"
    with ContainerWriter(path, {"whiteLevel": 4095.0}) as w:
        w.add_frame(1_000, b"a", json.dumps({"timestamp": "1000"}).encode())
        w.add_frame(2_000, b"b", frame_metadata)
        w.add_frame(3_000, b"c", json.dumps({"timestamp": "3000"}).encode())
    data = path.read_bytes()
    return data[: _index_start(data)]


@pytest.mark.parametrize(
    "hostile",
    [b"[" * 200_000, b'{"timestamp": "2000", "pad": "' + b"x" * (1 << 20) + b'"}'],
    ids=["nested", "oversized"],
)
def test_hostile_frame_metadata_stops_the_scan_there(tmp_path: Path, hostile: bytes) -> None:
    found = read_index(io.BytesIO(_hostile_container(tmp_path, hostile)))
    assert found.recovered
    assert [frame.timestamp_ns for frame in found.frames] == [1_000]


@pytest.mark.parametrize(
    "hostile", [b"[" * 200_000, b'{"pad": "' + b"x" * (1 << 20) + b'"}'], ids=["nested", "oversized"]
)
def test_hostile_container_metadata_is_refused_as_a_container_error(hostile: bytes) -> None:
    data = b"MOTION \x03" + struct.pack("<II", int(container.ItemType.METADATA), len(hostile)) + hostile
    with pytest.raises(ContainerError):
        read_index(io.BytesIO(data))


def test_cached_metadata_locations_skip_every_image_payload(tmp_path: Path) -> None:
    path = tmp_path / "indexed.mcraw"
    payload = b"do not read these pixels" * 4096
    with ContainerWriter(path, {}) as writer:
        for i in range(4):
            writer.add_frame(i, payload, json.dumps({"timestamp": str(i), "iso": 100 + i}).encode())

    class Reader(io.BytesIO):
        def __init__(self, data: bytes) -> None:
            super().__init__(data)
            self.reads: list[tuple[int, int]] = []

        def read(self, count: int | None = -1) -> bytes:
            start = self.tell()
            result = super().read(count)
            self.reads.append((start, start + len(result)))
            return result

    stream = Reader(path.read_bytes())
    index = read_index(stream)
    for frame in index.frames:
        assert frame.metadata_offset == frame.offset + 8 + len(payload)
        assert json.loads(container.read_frame_metadata(stream, frame.offset, ref=frame))["timestamp"] == str(
            frame.timestamp_ns
        )
        # Every read must be outside the compressed image interval.
        for start, end in stream.reads:
            assert end <= frame.offset + 8 or start >= frame.offset + 8 + len(payload)
    stream.reads.clear()
    for frame in index.frames:
        container.read_frame_metadata(stream, frame.offset, ref=frame)
    assert len(stream.reads) == 2 * len(index.frames)  # header and JSON only
    assert sum(end - start for start, end in stream.reads) < 1024


def test_cached_metadata_rechecks_headers_and_detects_truncation(tmp_path: Path) -> None:
    path = tmp_path / "changing.mcraw"
    _write_sample(path)
    data = path.read_bytes()
    frame = read_index(io.BytesIO(data)).frames[0]
    assert frame.metadata_offset is not None
    for kind, size in [(99, frame.metadata_size), (3, container.MAX_METADATA_BYTES + 1)]:
        damaged = bytearray(data)
        struct.pack_into("<II", damaged, frame.metadata_offset, kind, size)
        with pytest.raises(ContainerError):
            container.read_frame_metadata(io.BytesIO(damaged), frame.offset, ref=frame)
    with pytest.raises(ContainerError, match="end of file"):
        container.read_frame_metadata(io.BytesIO(data[: frame.metadata_offset + 9]), frame.offset, ref=frame)


def test_recovered_metadata_keeps_direct_locations(tmp_path: Path) -> None:
    path = tmp_path / "recovered.mcraw"
    _write_sample(path)
    data = path.read_bytes()
    stream = io.BytesIO(data[: _index_start(data)])
    index = read_index(stream)
    assert index.recovered
    for frame in index.frames:
        assert frame.metadata_offset is not None
        assert container.read_frame_metadata(
            stream, frame.offset, ref=frame
        ) == container.read_frame_metadata(stream, frame.offset)
