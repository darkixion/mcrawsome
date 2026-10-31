import json
import os
import struct
import time
import tracemalloc
from collections.abc import Callable, Sequence
from pathlib import Path

import numpy as np
import pytest
from click.testing import CliRunner
from conftest import write_mcraw

from mcrawsome.cli.main import cli
from mcrawsome.io.clip import Clip
from mcrawsome.io.container import ContainerWriter, read_audio_chunk, read_index
from mcrawsome.io.describe import describe
from mcrawsome.io.trim import trim


@pytest.mark.fixture
def test_clip_reads_frames_audio_and_rate(fixture_12bit: Path) -> None:
    with Clip(fixture_12bit) as clip:
        assert clip.frame_count == 3
        assert 23.9 < clip.fps < 24.1
        frame = clip.raw_frame(0)
        assert frame.data.shape == (2160, 3840)
        assert frame.data.dtype == np.uint16
        assert int(frame.data.min()) > 200
        assert int(frame.data.max()) <= 4095
        audio = clip.audio()
        assert audio is not None
        assert audio.sample_rate == 48000
        assert audio.samples.shape[1] == 2


@pytest.mark.fixture
def test_rotated_clip_reports_orientation(fixture_10bit_rotated: Path) -> None:
    with Clip(fixture_10bit_rotated) as clip:
        assert clip.frame_metadata(0).orientation == 3
        assert clip.metadata.white_level == 1023.0


def test_missing_file_gives_clear_error(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        Clip(tmp_path / "nope.mcraw")


def test_audio_without_timestamps_starts_at_the_first_frame(tmp_path: Path) -> None:
    meta = json.loads((Path(__file__).parent.parent / "data" / "container_metadata_pixel10.json").read_text())
    path = tmp_path / "a.mcraw"
    with ContainerWriter(path, meta) as w:
        for i in range(2):
            w.add_frame(5_000_000_000 + i * 41_666_666, b"\x01", b"{}")
            w.add_audio(np.zeros(8, np.int16).tobytes(), None)
    with Clip(path) as clip:
        audio = clip.audio()
        assert audio is not None
        assert audio.first_timestamp_ns == 5_000_000_000
        assert audio.samples.shape == (8, 2)


def _cut_before_index(path: Path) -> None:
    with path.open("r+b") as file:
        file.seek(-8, 2)
        (offset,) = struct.unpack("<q", file.read(8))
        file.truncate(offset - 8)


@pytest.fixture
def recovered_clip(tmp_path: Path) -> Path:
    data = Path(__file__).parent.parent / "data"
    metadata = json.loads((data / "container_metadata_174122.json").read_text())
    frame = json.loads((data / "frame_metadata_174122.json").read_text())
    path = tmp_path / "recovered.mcraw"
    with ContainerWriter(path, metadata) as writer:
        for i in range(3):
            timestamp = 5_000_000_000 + i * 41_666_666
            writer.add_frame(timestamp, b"image", json.dumps(frame | {"timestamp": str(timestamp)}).encode())
            writer.add_audio(np.zeros(8, np.int16).tobytes(), timestamp)
    _cut_before_index(path)
    return path


def test_a_recovered_clip_reports_its_frames_audio_and_note(recovered_clip: Path) -> None:
    with Clip(recovered_clip) as clip:
        assert clip.recovered
        assert clip.frame_count == 3
        assert clip.audio_chunk_count == 3
        assert clip.fps == pytest.approx(24)
        assert clip.recovery_note == (
            "Recovered: the recording was not finalised; 3 frames were found by scanning"
        )
        sections = dict(describe(clip))
        assert clip.recovery_note in dict(sections["Recording"]).values()
        audio = clip.audio()
        assert audio is not None
        assert audio.first_timestamp_ns == 5_000_000_000


def test_a_clip_with_a_damaged_index_says_so(tmp_path: Path) -> None:
    data = Path(__file__).parent.parent / "data"
    metadata = json.loads((data / "container_metadata_174122.json").read_text())
    frame = json.loads((data / "frame_metadata_174122.json").read_text())
    path = tmp_path / "damaged.mcraw"
    with ContainerWriter(path, metadata) as writer:
        for i in range(2):
            timestamp = 5_000_000_000 + i * 41_666_666
            writer.add_frame(timestamp, b"image", json.dumps(frame | {"timestamp": str(timestamp)}).encode())
    damaged = bytearray(path.read_bytes())
    damaged[-16] ^= 0xFF  # the first byte of the index magic number
    path.write_bytes(bytes(damaged))
    with Clip(path) as clip:
        assert clip.recovery_note == "Recovered: the frame index was damaged; 2 frames were found by scanning"


def test_info_prints_the_recovery_note(recovered_clip: Path) -> None:
    result = CliRunner().invoke(cli, ["info", str(recovered_clip)])
    assert result.exit_code == 0, result.output
    assert "Recovered:" in result.output
    assert "3 frames were found by scanning" in " ".join(result.output.split())


@pytest.fixture
def unfinalised_local() -> Path:
    folder = Path(os.environ.get("MCRAWSOME_LOCAL_SAMPLES", Path.home() / "Downloads" / "motioncam"))
    path = folder / "261010_135132_VIDEO_25mm.mcraw"
    if not path.is_file():
        pytest.skip("local unfinished recording not present (set MCRAWSOME_LOCAL_SAMPLES)")
    return path


@pytest.mark.local
def test_local_unfinished_recording_opens_in_under_ten_seconds(unfinalised_local: Path) -> None:
    before = time.perf_counter()
    with Clip(unfinalised_local) as clip:
        assert time.perf_counter() - before < 10
        assert clip.recovered
        timestamps = clip.timestamps_ns
        expected = (int(timestamps[-1]) - int(timestamps[0])) / 1e9 * clip.fps
        assert clip.frame_count == pytest.approx(expected, abs=max(2, expected * 0.05))


@pytest.mark.local
def test_info_on_a_local_copy_cut_before_its_index(unfinalised_local: Path, tmp_path: Path) -> None:
    count = 24  # a small slice, so the scratch copy stays tiny; the original is only read
    copy = tmp_path / "cut.mcraw"
    trim(unfinalised_local, copy, 0, count)
    _cut_before_index(copy)
    result = CliRunner().invoke(cli, ["info", str(copy)])
    assert result.exit_code == 0, result.output
    assert "Recovered:" in result.output
    assert f"{count} frames were found by scanning" in " ".join(result.output.split())


@pytest.fixture
def audio_chunks_clip(tmp_path: Path) -> Callable[[Sequence[tuple[int | None, int, int]], int], Path]:
    def make(chunks: Sequence[tuple[int | None, int, int]], rate: int = 1000) -> Path:
        source = write_mcraw(tmp_path / "source.mcraw", [1_000_000_000 + i * 250_000_000 for i in range(3)])
        with Clip(source) as clip:
            metadata = clip.raw_metadata
            frames = [(int(clip.timestamps_ns[i]), clip.raw_frame_metadata(i)) for i in range(3)]
        from conftest import _payload

        metadata["extraData"] |= {"audioSampleRate": rate, "audioChannels": 1}
        with ContainerWriter(source, metadata) as writer:
            for index, (stamp, frame) in enumerate(frames):
                writer.add_frame(stamp, _payload(100 + 40 * index), json.dumps(frame).encode())
            for timestamp, count, marker in chunks:
                writer.add_audio(np.full(count, marker, "<i2").tobytes(), timestamp)
        return source

    return make


@pytest.fixture
def gapped_audio_clip(
    audio_chunks_clip: Callable[[Sequence[tuple[int | None, int, int]], int], Path],
) -> Callable[[int | None], Path]:
    return lambda timestamp: audio_chunks_clip([(1_000_000_000, 20, 1000), (timestamp, 20, 2000)], 1000)


@pytest.mark.parametrize(("timestamp", "position"), [(1_020_000_000, 20), (1_220_000_000, 220)])
def test_audio_chunk_timestamps_position_the_second_marker(
    gapped_audio_clip: Callable[[int | None], Path], timestamp: int, position: int
) -> None:
    with Clip(gapped_audio_clip(timestamp)) as clip:
        track = clip.audio()
    assert track is not None
    assert np.flatnonzero(track.samples[:, 0] == 2000)[0] == position
    assert not track.samples[20:position].any()


@pytest.mark.parametrize("chunk_samples", [20, 100])
def test_audio_timestamp_jitter_keeps_plain_concatenation(
    audio_chunks_clip: Callable[[Sequence[tuple[int | None, int, int]], int], Path], chunk_samples: int
) -> None:
    chunks = [
        (
            1_000_000_000 + i * chunk_samples * 1_000_000 + (20_000_000 if i % 2 else -20_000_000),
            chunk_samples,
            i + 1,
        )
        for i in range(20)
    ]
    with Clip(audio_chunks_clip(chunks, 1000)) as clip:
        track = clip.audio()
    assert track is not None
    expected = np.concatenate([np.full(count, marker, np.int16) for _, count, marker in chunks])
    np.testing.assert_array_equal(track.samples[:, 0], expected)


@pytest.mark.parametrize(
    ("offset_ms", "chunk_samples", "expected_count"),
    [
        (50, 20, 40),
        (51, 20, 91),
        (100, 100, 200),
        (101, 100, 301),
        (-20, 20, 40),
        (-60, 100, 200),
        (-110, 100, 100),
    ],
)
def test_audio_discontinuity_tolerance(
    audio_chunks_clip: Callable[[Sequence[tuple[int | None, int, int]], int], Path],
    offset_ms: int,
    chunk_samples: int,
    expected_count: int,
) -> None:
    first = 1_000_000_000
    chunks: list[tuple[int | None, int, int]] = [
        (first, chunk_samples, 1),
        (first + (chunk_samples + offset_ms) * 1_000_000, chunk_samples, 2),
    ]
    # Include an untimed chunk so overlapping timestamps stay in container order.
    chunks.append((None, 1, 3))
    with Clip(audio_chunks_clip(chunks, 1000)) as clip:
        track = clip.audio()
    assert track is not None
    assert len(track.samples) == expected_count + 1
    assert track.samples[-1, 0] == 3


def test_a_day_late_audio_timestamp_is_capped(
    audio_chunks_clip: Callable[[Sequence[tuple[int | None, int, int]], int], Path],
    caplog: pytest.LogCaptureFixture,
) -> None:
    path = audio_chunks_clip([(1_000_000_000, 2, 1), (86_401_000_000_000, 2, 2)], 10)
    with Clip(path) as clip:
        tracemalloc.start()
        try:
            track = clip.audio()
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
    assert peak < 64_000
    assert track is not None
    assert track.samples[:, 0].tolist() == [1, 1, 2, 2]
    assert "corrupt audio timestamp" in caplog.text


def test_mixed_timed_and_untimed_audio_keeps_container_order(
    audio_chunks_clip: Callable[[Sequence[tuple[int | None, int, int]], int], Path],
) -> None:
    chunks = [(1_000_000_000, 20, 1), (None, 20, 2), (1_020_000_000, 20, 3), (None, 20, 4)]
    with Clip(audio_chunks_clip(chunks, 1000)) as clip:
        track = clip.audio()
    assert track is not None
    assert track.first_timestamp_ns == 1_000_000_000
    assert track.samples[:, 0].tolist() == [1] * 20 + [2] * 20 + [3] * 20 + [4] * 20


@pytest.mark.local
def test_local_174122_audio_is_plain_concatenation() -> None:
    from conftest import LOCAL_DIR

    path = LOCAL_DIR / "261004_174122_VIDEO_26mm.mcraw"
    if not path.is_file():
        pytest.skip("local 261004_174122 recording not present (set MCRAWSOME_LOCAL_SAMPLES)")
    with Clip(path) as clip:
        channels = clip.metadata.audio_channels or 1
        with path.open("rb") as stream:
            index = read_index(stream)
            blocks = [read_audio_chunk(stream, ref.offset)[1] for ref in index.audio]
        expected = np.concatenate([block[: len(block) - len(block) % channels] for block in blocks])
        track = clip.audio()
    assert track is not None
    assert track.samples.size == expected.size
    np.testing.assert_array_equal(track.samples.reshape(-1), expected)
