import io
import struct
from pathlib import Path

import numpy as np
import pytest

from mcrawsome.io.movie_metadata import MovieMetadataError, boxes, read_movie_metadata, read_movie_telemetry


def box(tag: bytes, body: bytes) -> bytes:
    return struct.pack(">I4s", len(body) + 8, tag) + body


def table(tag: bytes, *rows: tuple[int, ...], version: int = 0) -> bytes:
    return box(
        tag,
        bytes([version, 0, 0, 0])
        + struct.pack(">I", len(rows))
        + b"".join(struct.pack(">" + "I" * len(row), *row) for row in rows),
    )


def movie(tmp_path: Path, *, ctts: bool = False) -> tuple[Path, tuple[int, int]]:
    image = b"unread image data" * 4096
    telemetry = b"DEVCtestSTRMtest"
    payload = image + telemetry[:8] + b"audio" * 100 + telemetry[8:]
    offset = 8 + len(image)
    mdhd = box(b"mdhd", b"\0" * 12 + struct.pack(">II", 1_000_000, 120_000))
    stts = table(b"stts", (1, 40001), (1, 39999), (1, 40000))
    if ctts:
        stts = table(b"stts", (3, 40000)) + table(b"ctts", (1, 80000), (2, (1 << 32) - 40000), version=1)
    video = box(
        b"trak", box(b"mdia", mdhd + box(b"hdlr", b"\0" * 8 + b"vide") + box(b"minf", box(b"stbl", stts)))
    )
    samples = (
        box(b"stsd", b"\0" * 4 + struct.pack(">I", 1) + box(b"gpmd", b""))
        + box(b"stsz", b"\0" * 4 + struct.pack(">II", 8, 2))
        + table(b"stco", (offset,), (offset + 8 + 500,))
        + table(b"stsc", (1, 1, 1))
    )
    gyro = box(b"trak", box(b"mdia", box(b"hdlr", b"\0" * 8 + b"meta") + box(b"minf", box(b"stbl", samples))))
    path = tmp_path / "test.mp4"
    path.write_bytes(box(b"mdat", payload) + box(b"moov", video + gyro))
    return path, (8, 8 + len(image))


def test_movie_tables_preserve_sub_microsecond_timing_and_skip_video(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, image = movie(tmp_path)
    original = path.read_bytes()
    reads = []

    class Reader(io.BytesIO):
        def read(self, count: int | None = -1) -> bytes:
            start = self.tell()
            result = super().read(count)
            reads.append((start, start + len(result)))
            return result

    monkeypatch.setattr(Path, "open", lambda *a, **k: Reader(original))
    result = read_movie_metadata(path)
    np.testing.assert_array_equal(result.times_ns, [0, 40_001_000, 80_000_000])
    assert read_movie_telemetry(path) == b"DEVCtestSTRMtest"
    for start, end in reads:
        assert end <= image[0] or start >= image[1]
    assert sum(end - start for start, end in reads) < 2048


def test_composition_time_reorders_b_frames_in_presentation_order(tmp_path: Path) -> None:
    path, _ = movie(tmp_path, ctts=True)
    np.testing.assert_array_equal(read_movie_metadata(path).times_ns, [0, 40_000_000, 80_000_000])


@pytest.mark.parametrize(
    "data",
    [
        b"x",
        struct.pack(">I4s", 7, b"moov"),
        struct.pack(">I4s", 1, b"moov"),
        struct.pack(">I4s", 500, b"moov"),
    ],
)
def test_bad_movie_box_sizes_are_rejected(data: bytes) -> None:
    with pytest.raises(MovieMetadataError):
        boxes(data)


def test_oversized_timing_repeat_rejected_before_allocation(tmp_path: Path) -> None:
    path, _ = movie(tmp_path)
    data = bytearray(path.read_bytes())
    offset = data.index(b"stts") + 12
    struct.pack_into(">I", data, offset, 2_000_001)
    path.write_bytes(data)
    with pytest.raises(MovieMetadataError, match="two million"):
        read_movie_metadata(path)


def test_fragmented_movie_does_not_invent_nominal_timing(tmp_path: Path) -> None:
    path = tmp_path / "fragmented.mp4"
    path.write_bytes(box(b"moof", b""))
    with pytest.raises(MovieMetadataError, match="Fragmented"):
        read_movie_metadata(path)
