"""Playback readers can interleave byte ranges from neighbouring full-resolution frames."""

import threading
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from mcrawsome.mount.limits import CACHE_FRAMES, IDLE_RELEASE_S
from mcrawsome.mount.manager import MountedSequence, MountManager, availability
from mcrawsome.mount.sequence import Sequence, source_stamp
from mcrawsome.mount.service import ServingSequence


def test_interleaved_sensor_frame_reads_do_not_redecode_each_byte_range(
    make_mcraw: Callable[..., Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A 4:3 sensor frame is larger than half of the former 32 MiB service cache.
    width, height = 4096, 3072
    source = make_mcraw(tmp_path / "sensor.mcraw", [i * 41666667 for i in range(3)], size=(width, height))
    record = MountedSequence(id="0" * 24, source=source, path=tmp_path / "media", stamp=source_stamp(source))
    folder = tmp_path / "state" / record.id
    folder.mkdir(parents=True)
    decoded: list[int] = []
    original = ServingSequence._decode

    def decode(sequence: ServingSequence, index: int, factor: int | None = None):  # type: ignore[no-untyped-def]
        decoded.append(index)
        return original(sequence, index, factor)

    monkeypatch.setattr(ServingSequence, "_decode", decode)
    with ServingSequence(record, folder) as sequence:
        names = sequence.names()[:3]
        offsets = [sequence.size(name) - width * height * 2 for name in names]
        for chunk in range(12):
            for name, offset in zip(names, offsets, strict=True):
                assert len(sequence.read(name, offset + chunk * 131072, 131072)) == 131072
        assert decoded == [0, 1, 2], "interleaved reads repeatedly evicted and decoded the same frames"
        assert sequence.cached_bytes <= sequence.limit


def test_frames_an_editor_reads_at_once_are_decoded_once_however_large(
    make_mcraw: Callable[..., Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Resolve reads eight frames at once. A byte budget held five 12.5-megapixel frames, so each was
    # decoded again for nearly every chunk. Leave only the frame-count floor.
    monkeypatch.setattr("mcrawsome.mount.service.CACHE_BYTES", 1)
    width, height = 64, 48
    times = [i * 41666667 for i in range(CACHE_FRAMES)]
    source = make_mcraw(tmp_path / "frames.mcraw", times, size=(width, height))
    record = MountedSequence(id="0" * 24, source=source, path=tmp_path / "media", stamp=source_stamp(source))
    folder = tmp_path / "state" / record.id
    folder.mkdir(parents=True)
    decoded: list[int] = []
    original = ServingSequence._decode

    def decode(sequence: ServingSequence, index: int, factor: int | None = None):  # type: ignore[no-untyped-def]
        decoded.append(index)
        return original(sequence, index, factor)

    monkeypatch.setattr(ServingSequence, "_decode", decode)
    with ServingSequence(record, folder) as sequence:
        names = [name for name in sequence.names() if name.endswith(".dng")]
        assert len(names) == CACHE_FRAMES
        offsets = [sequence.size(name) - width * height * 2 for name in names]
        for at in range(0, width * height * 2, 1024):
            for name, offset in zip(names, offsets, strict=True):
                assert len(sequence.read(name, offset + at, 1024)) == 1024
        assert sorted(decoded) == list(range(CACHE_FRAMES)), "frames read at once evicted each other"


def _small_sequence(make_mcraw: Callable[..., Path], tmp_path: Path) -> MountedSequence:
    source = make_mcraw(tmp_path / "idle.mcraw", [i * 41666667 for i in range(3)], size=(64, 48))
    record = MountedSequence(id="0" * 24, source=source, path=tmp_path / "media", stamp=source_stamp(source))
    (tmp_path / "state" / record.id).mkdir(parents=True)
    return record


def test_an_idle_mount_lets_go_of_its_frames_and_serves_them_again(
    make_mcraw: Callable[..., Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = _small_sequence(make_mcraw, tmp_path)
    trims: list[bool] = []
    monkeypatch.setattr("mcrawsome.mount.service.trim_heap", lambda: trims.append(True))
    with ServingSequence(record, tmp_path / "state" / record.id) as sequence:
        name = next(name for name in sequence.names() if name.endswith(".dng"))
        end = sequence.size(name) - 1024
        served = bytes(sequence.read(name, end, 1024))
        assert sequence.cached_bytes
        assert not sequence.release_if_idle(time.monotonic()), "frames went while they were being read"
        assert sequence.release_if_idle(time.monotonic() + IDLE_RELEASE_S)
        assert (sequence.cached_bytes, len(sequence.pixels), trims) == (0, 0, [True])
        assert not sequence.release_if_idle(time.monotonic() + IDLE_RELEASE_S), "nothing was left to let go"
        assert bytes(sequence.read(name, end, 1024)) == served
        assert sequence.cached_bytes


def test_a_mount_lets_go_of_its_frames_by_itself_and_stops_checking_when_closed(
    make_mcraw: Callable[..., Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("mcrawsome.mount.service.IDLE_RELEASE_S", 0.05)
    monkeypatch.setattr("mcrawsome.mount.service.IDLE_CHECK_S", 0.01)
    record = _small_sequence(make_mcraw, tmp_path)
    before = set(threading.enumerate())
    with ServingSequence(record, tmp_path / "state" / record.id) as sequence:
        checks = [
            thread for thread in threading.enumerate() if thread.name == "mount-idle" and thread not in before
        ]
        name = next(name for name in sequence.names() if name.endswith(".dng"))
        sequence.read(name, sequence.size(name) - 1024, 1024)
        deadline = time.monotonic() + 5
        while sequence.cached_bytes and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not sequence.cached_bytes, "an idle mount kept its frames"
    assert len(checks) == 1
    assert not checks[0].is_alive(), "the idle check outlived its mount"


def test_native_fuse_reads_buffer_views_boundaries_and_mmap(tiny_raw: Path) -> None:
    import mmap

    if not availability()[0]:
        pytest.skip("FUSE unavailable")
    manager = MountManager()
    record = manager.mount(tiny_raw)
    try:
        with Sequence(tiny_raw) as expected:
            for name in expected.names():
                size = expected.size(name)
                wanted = b"".join(expected.read(name, at, 997) for at in range(0, size, 997))
                path = record.path / name
                assert path.read_bytes() == wanted
                with (
                    path.open("rb") as stream,
                    mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as view,
                ):
                    assert view[:] == wanted
                    assert view[max(0, size - 123) :] == wanted[max(0, size - 123) :]
    finally:
        manager.unmount(record.id)


@pytest.mark.parametrize("shading", ["opcode", "baked"])
def test_read_ahead_preserves_complete_dng_and_audio_bytes_with_dropped_frame_fill(
    make_mcraw: Callable[..., Path], tmp_path: Path, shading: str
) -> None:
    from mcrawsome.recipe.raw import RawCalibration

    source = make_mcraw(tmp_path / "gaps.mcraw", [i * 40_000_000 for i in (0, 1, 4, 5, 6)])
    settings = RawCalibration(
        rotation=90, black_level=32, white_level=3000, lens_shading=0.5, mount_shading=shading
    )
    record = MountedSequence(
        id="0" * 24, source=source, path=tmp_path / "media", stamp=source_stamp(source), raw_settings=settings
    )
    folder = tmp_path / "state" / record.id
    folder.mkdir(parents=True)
    with ServingSequence(record, folder) as sequence, Sequence(source, raw_settings=settings) as expected:
        assert sequence.sources == [0, 1, 1, 1, 2, 3, 4]
        names = sequence.names()
        for name in [*names, names[0], names[3], names[1]]:
            size = sequence.size(name)
            wanted = b"".join(expected.read(name, at, 32768) for at in range(0, size, 32768))
            # Header lookups interleave with the independent background frame reader.
            sequence.read(names[0], 0, 8)
            actual = b"".join(sequence.read(name, at, 4096) for at in range(0, size, 4096))
            assert actual == wanted
            assert sequence.cached_bytes <= sequence.limit
        assert sequence._ahead_clips
    assert sequence._ahead_clips == {}


def test_header_only_service_reads_do_not_start_speculative_decoding(
    tiny_raw: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mcrawsome.io.clip import Clip

    monkeypatch.setattr(Clip, "raw_frame", lambda *_: pytest.fail("decoded pixels for a header"))
    record = MountedSequence(
        id="0" * 24, source=tiny_raw, path=tmp_path / "media", stamp=source_stamp(tiny_raw)
    )
    folder = tmp_path / "state" / record.id
    folder.mkdir(parents=True)
    with ServingSequence(record, folder) as sequence:
        for name in sequence.names()[:-1]:
            sequence.size(name)
            assert sequence.read(name, 0, 8) == b"II*\0\x08\0\0\0"
        assert sequence._ahead_clips == {}


def test_source_change_rejects_even_a_prepared_frame(tiny_raw: Path, tmp_path: Path) -> None:
    record = MountedSequence(
        id="0" * 24, source=tiny_raw, path=tmp_path / "media", stamp=source_stamp(tiny_raw)
    )
    folder = tmp_path / "state" / record.id
    folder.mkdir(parents=True)
    with ServingSequence(record, folder) as sequence:
        names = sequence.names()
        for name in names[:2]:
            sequence.read(name, sequence.size(name) - 2, 2)
        with tiny_raw.open("ab") as stream:
            stream.write(b"changed")
        with pytest.raises(OSError, match="changed or is offline"):
            sequence.read(names[2], 0, 8)
