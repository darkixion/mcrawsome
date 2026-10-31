import struct
from pathlib import Path

import numpy as np
import pytest

from mcrawsome.io.gpmf import GpmfError, extract_gpmf, gyro_log_from_gpmf, read_gpmf
from mcrawsome.output.ffmpeg import find_ffmpeg


def klv(key: str, kind: str, size: int, repeat: int, payload: bytes) -> bytes:
    body = struct.pack(">4scBH", key.encode(), kind.encode(), size, repeat) + payload
    return body + b"\0" * (-len(payload) % 4)


def nest(key: str, *children: bytes) -> bytes:
    payload = b"".join(children)
    return klv(key, "\0", 1, len(payload), payload)


def text(key: str, value: str) -> bytes:
    return klv(key, "c", 1, len(value), value.encode())


def stream(name: str, stmp_us: int, scale: int, samples: np.ndarray) -> bytes:
    flat = samples.astype(">i2").tobytes()
    return nest(
        "STRM",
        klv("STMP", "J", 8, 1, struct.pack(">Q", stmp_us)),
        klv("TSMP", "L", 4, 1, struct.pack(">I", len(samples))),
        text("STNM", name),
        text("ORIN", "ZXY"),
        text("ORIO", "XYZ"),
        klv("SCAL", "s", 2, 1, struct.pack(">h", scale)),
        klv(name[:4], "s", 6, len(samples), flat),
    )


def telemetry(blocks: int = 3, per_block: int = 4) -> bytes:
    header = nest(
        "DEVC", text("DVNM", "MotionCam"), text("MINF", "pixel 10 pro xl 25.37mm f/1.7"), text("EISA", "N")
    )
    data = b""
    for b in range(blocks):
        gyro = np.array([[100 * b + i, 10, -20] for i in range(per_block)])
        accel = np.array([[1962, 0, 500]] * per_block)
        data += nest(
            "DEVC",
            text("DVNM", "MotionCam"),
            stream("GYRO", b * 41667, 1000, gyro),
            stream("ACCL", b * 41667, 200, accel),
        )
    return struct.pack(">I", len(header)) + b"GPMF" + header + data


def test_reads_blocks_scale_and_device_info() -> None:
    streams = read_gpmf(telemetry())
    assert streams.device["MINF"] == "pixel 10 pro xl 25.37mm f/1.7"
    assert streams.device["EISA"] == "N"
    assert len(streams.blocks) == 3
    assert streams.orin == "ZXY"


def test_gyro_log_is_reordered_scaled_and_timed() -> None:
    log = gyro_log_from_gpmf(read_gpmf(telemetry()))
    assert log.clock == "video"
    assert log.gyro.shape == (12, 3)
    # ORIN ZXY -> ORIO XYZ: stored channels are (Z, X, Y)
    np.testing.assert_allclose(log.gyro[1], [10 / 1000, -20 / 1000, 1 / 1000])
    assert log.accel is not None
    np.testing.assert_allclose(log.accel[0], [0.0, 500 / 200, 1962 / 200])
    np.testing.assert_allclose(
        log.times_s[:5], [0, 41667e-6 / 4, 2 * 41667e-6 / 4, 3 * 41667e-6 / 4, 41667e-6]
    )
    assert np.all(np.diff(log.times_s) > 0)


def test_unknown_keys_are_skipped() -> None:
    data = telemetry() + nest("DEVC", text("ZZZZ", "future"))
    assert len(read_gpmf(data).blocks) == 3


def test_truncated_data_names_the_offset() -> None:
    with pytest.raises(GpmfError, match="offset"):
        read_gpmf(telemetry()[:-30])


@pytest.mark.local
@pytest.mark.parametrize("sample", ["direct_log_davinci", "direct_log_bt709"])
def test_real_direct_log_telemetry(sample: str, request: pytest.FixtureRequest) -> None:
    path: Path = request.getfixturevalue(sample)
    data = extract_gpmf(find_ffmpeg()[0], path)
    assert data is not None
    streams = read_gpmf(data)
    assert streams.device["MINF"].startswith("pixel 10 pro xl")
    log = gyro_log_from_gpmf(streams)
    per_frame = len(log.times_s) / len(streams.blocks)
    assert 15 <= per_frame <= 17
    assert float(np.abs(log.gyro).max()) < 20  # rad/s: a plausible hand-held range


def test_empty_blocks_keep_the_timing() -> None:
    data = telemetry(blocks=3)
    empty = nest(
        "DEVC",
        text("DVNM", "MotionCam"),
        nest(
            "STRM",
            klv("STMP", "J", 8, 1, struct.pack(">Q", 3 * 41667)),
            klv("EMPT", "L", 4, 1, struct.pack(">I", 1)),
            klv("SCAL", "s", 2, 1, struct.pack(">h", 1000)),
            struct.pack(">4scBH", b"GYRO", b"\xfe", 6, 0),
        ),
    )
    late = telemetry(blocks=1).split(b"GPMF", 1)[1]
    late = late[late.index(b"DEVC", 8) :].replace(struct.pack(">Q", 0), struct.pack(">Q", 4 * 41667), 2)
    log = gyro_log_from_gpmf(read_gpmf(data + empty + late))
    assert len(log.times_s) == 16
    assert log.times_s[12] == pytest.approx(4 * 41667e-6)  # after the empty block, not squeezed into it
