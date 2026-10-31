import hashlib
import struct
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

from mcrawsome import _raw
from mcrawsome.io.clip import Clip
from mcrawsome.io.container import read_frame_record


def _metadata(values: list[int], padded_count: bool = True) -> bytes:
    padded = values + [0] * (-len(values) % 64)
    count = len(padded) if padded_count else len(values)
    return struct.pack("<I", count) + b"".join(
        b"\xf0\x00" + struct.pack("<64H", *padded[i : i + 64]) for i in range(0, len(padded), 64)
    )


def _frame(bits: int = 16, height: int = 4) -> bytes:
    blocks = height
    image = struct.pack("<64H", *range(64)) * blocks if bits else b""
    bit_table = _metadata([bits] * blocks)
    references = _metadata([0] * blocks)
    offset = 16 + len(image)
    return struct.pack("<4I", 64, height, offset, offset + len(bit_table)) + image + bit_table + references


def _legacy(height: int = 4) -> bytes:
    block = b"\xf0\x00" + struct.pack(">16H", *range(16))
    return block * (4 * height) + b"\x00"


def _replace_u32(data: bytes, offset: int, value: int) -> bytes:
    changed = bytearray(data)
    struct.pack_into("<I", changed, offset, value)
    return bytes(changed)


@pytest.mark.parametrize("compression", [6, 7])
@pytest.mark.parametrize("length", [0, 1, 15, 16, -1])
def test_rejects_truncated_data(compression: int, length: int) -> None:
    data = _legacy() if compression == 6 else _frame()
    data = data[: len(data) // 2 if length == -1 else length]
    with pytest.raises(ValueError, match="frame"):
        _raw.decode(data, 64, 4, compression)


@pytest.mark.parametrize("compression", [6, 7])
def test_rejects_encoded_height_smaller_than_clip(compression: int) -> None:
    data = _legacy() if compression == 6 else _frame()
    with pytest.raises(ValueError, match="frame"):
        _raw.decode(data, 64, 8, compression)


@pytest.mark.parametrize("compression", [6, 7])
def test_rejects_encoded_height_larger_than_clip(compression: int) -> None:
    data = _legacy(8) if compression == 6 else _frame(height=8)
    with pytest.raises(ValueError, match="frame"):
        _raw.decode(data, 64, 4, compression)


@pytest.mark.parametrize("offset", [8, 12])
@pytest.mark.parametrize("past", [0, 1, 0xFFFFFFFF])
def test_rejects_metadata_offsets_outside_payload(offset: int, past: int) -> None:
    data = _frame()
    value = len(data) + past if past != 0xFFFFFFFF else past
    with pytest.raises(ValueError, match="frame"):
        _raw.decode(_replace_u32(data, offset, value), 64, 4, 7)


@pytest.mark.parametrize(("bits", "refs"), [(0, 16), (8, 16), (16, 16), (32, 16)])
def test_rejects_overlapping_metadata_sections(bits: int, refs: int) -> None:
    data = _replace_u32(_replace_u32(_frame(), 8, bits), 12, refs)
    with pytest.raises(ValueError, match="frame"):
        _raw.decode(data, 64, 4, 7)


@pytest.mark.parametrize("section", ["image", "bits", "refs"])
def test_rejects_truncated_sections_with_corrected_offsets(section: str) -> None:
    data = _frame()
    _, _, bits, refs = struct.unpack_from("<4I", data)
    if section == "image":
        data = data[: bits - 1] + data[bits:]
        data = _replace_u32(_replace_u32(data, 8, bits - 1), 12, refs - 1)
    elif section == "bits":
        data = _replace_u32(data[: refs - 1] + data[refs:], 12, refs - 1)
    else:
        data = data[:-1]
    with pytest.raises(ValueError, match="frame data is truncated"):
        _raw.decode(data, 64, 4, 7)


@pytest.mark.parametrize("table", [2, 3])
@pytest.mark.parametrize("count", [0, 1, 2, 3, 65, 128, 0xFFFFFFFF])
def test_rejects_invalid_metadata_counts(table: int, count: int) -> None:
    data = _frame()
    offset = struct.unpack_from("<4I", data)[table]
    with pytest.raises(ValueError, match=r"metadata count|data is truncated"):
        _raw.decode(_replace_u32(data, offset, count), 64, 4, 7)


@pytest.mark.parametrize(("width", "height"), [(0, 4), (63, 4), (32, 4), (64, 0), (64, 2), (64, 6)])
def test_rejects_invalid_frame_header_dimensions(width: int, height: int) -> None:
    data = _replace_u32(_replace_u32(_frame(), 0, width), 4, height)
    with pytest.raises(ValueError, match="frame"):
        _raw.decode(data, 64, 4, 7)


def test_rejects_image_bit_depth_outside_table() -> None:
    data = _frame()
    bits = struct.unpack_from("<4I", data)[2]
    changed = bytearray(data)
    struct.pack_into("<H", changed, bits + 6, 17)
    with pytest.raises(ValueError, match="bit depth"):
        _raw.decode(bytes(changed), 64, 4, 7)


@pytest.mark.parametrize("suffix", [b"\xff", b"\xff" * 5, struct.pack(">I", 0xFFFFFFFF) + b"\xff"])
def test_rejects_invalid_legacy_offset_trailer(suffix: bytes) -> None:
    with pytest.raises(ValueError, match="frame offset"):
        _raw.decode(_legacy() + suffix, 64, 4, 6)


@pytest.mark.parametrize("compression", [6, 7])
def test_decodes_complete_synthetic_frames(compression: int) -> None:
    data = _legacy() if compression == 6 else _frame()
    image = _raw.decode(data, 64, 4, compression)
    assert image.shape == (4, 64)
    assert image.dtype == np.uint16
    assert int(image.max()) == (15 if compression == 6 else 63)


def test_decodes_zero_bit_blocks_and_cropped_width() -> None:
    image = _raw.decode(_frame(bits=0), 32, 4, 7)
    assert image.shape == (4, 32)
    assert not image.any()


def test_decodes_a_legacy_frame_with_an_offset_trailer() -> None:
    data = _legacy() + struct.pack(">I", 0) + b"\xff"
    np.testing.assert_array_equal(_raw.decode(data, 64, 4, 6), _raw.decode(_legacy(), 64, 4, 6))


def test_rejects_short_metadata_before_allocating_output() -> None:
    script = """
import resource
import struct
from mcrawsome import _raw
import numpy as np
np.zeros(1)
limit = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024 + 128 * 1024 * 1024
resource.setrlimit(resource.RLIMIT_DATA, (limit, limit))
data = struct.pack('<4I', 64, 16777216, 16, 22) + struct.pack('<I', 64) + b'\\x00\\x00'
try:
    _raw.decode(data, 64, 16777216, 7)
except ValueError:
    pass
else:
    raise AssertionError('malformed metadata was accepted')
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr


_HASHES = {
    "clip_12bit.mcraw": [
        "57fcdd6f1f21741f1617e9ca1dd2703f93734aeb60423ea21276026ceeb3c6eb",
        "d50223cd62c52dddbba8820842f5eebd8d783b62fe41904c83a156905ed4f399",
        "a0e75636a8609a3183b50379594891b83b1bd6e4fb92586ff33d8dbf83b6540e",
    ],
    "clip_10bit_rotated.mcraw": [
        "8d9b4b3719f6196e3246cfea0a6782601ed80a094382946d26254ec5108cded6",
        "6745d532c131829d0d2389973f8ec6c5ceb960ff1bef1c8bbd3b8ac14697eeef",
        "01cee8e22434b7f5cc7eeb7f81fb2f577dad613110cccb98c030edb6b60bf34e",
    ],
}


@pytest.mark.fixture
@pytest.mark.parametrize("fixture_name", ["fixture_12bit", "fixture_10bit_rotated"])
def test_fixture_frames_keep_their_hashes(fixture_name: str, request: pytest.FixtureRequest) -> None:
    path: Path = request.getfixturevalue(fixture_name)
    with Clip(path) as clip:
        hashes = [
            hashlib.sha256(clip.raw_frame(i).data.tobytes()).hexdigest() for i in range(clip.frame_count)
        ]
    assert hashes == _HASHES[path.name]


@pytest.mark.fixture
def test_rejects_half_of_a_real_frame(fixture_12bit: Path) -> None:
    with fixture_12bit.open("rb") as f, Clip(fixture_12bit) as clip:
        # The first frame follows the container header and its metadata item.
        f.seek(8)
        _, length = struct.unpack("<II", f.read(8))
        payload, _ = read_frame_record(f, 16 + length)
        meta = clip.frame_metadata(0)
    with pytest.raises(ValueError, match="frame data is truncated"):
        _raw.decode(payload[: len(payload) // 2], meta.width, meta.height, meta.compression_type)


def _wide_frame(width: int, height: int, padded_count: bool, extra: int = 0, tail: bytes = b"") -> bytes:
    blocks = width // 64 * height
    image = b"".join(struct.pack("<64H", *range(64)) for _ in range(blocks))
    bit_table = _metadata([16] * blocks, padded_count)
    references = _metadata([0] * blocks, padded_count)
    offset = 16 + len(image)
    header = struct.pack("<4I", width + extra, height, offset, offset + len(bit_table))
    return header + image + bit_table + references + tail


@pytest.mark.parametrize("padded_count", [False, True])
def test_decodes_metadata_counts_not_a_multiple_of_the_block_size(padded_count: bool) -> None:
    # 128 x 12 needs 24 blocks, so the unpadded count is not a multiple of 64.
    image = _raw.decode(_wide_frame(128, 12, padded_count), 128, 12, 7)
    # Every block holds 0..63; the decoder spreads each block over two rows of its row group.
    y, x = np.mgrid[0:12, 0:128]
    expected = ((y % 4 // 2) * 32 + x % 64 // 2).astype(np.uint16)
    assert np.array_equal(image, expected)


def test_rejects_metadata_count_smaller_than_the_blocks_needed() -> None:
    data = _wide_frame(128, 12, False)
    offset = struct.unpack_from("<4I", data)[2]
    with pytest.raises(ValueError, match="metadata count"):
        _raw.decode(_replace_u32(data, offset, 23), 128, 12, 7)


def test_rejects_encoded_width_a_block_wider_than_the_clip() -> None:
    with pytest.raises(ValueError, match="frame width"):
        _raw.decode(_wide_frame(128, 4, True, extra=64), 128, 4, 7)


def test_accepts_trailing_bytes_after_the_refs_table() -> None:
    plain = _raw.decode(_wide_frame(128, 12, True), 128, 12, 7)
    assert np.array_equal(_raw.decode(_wide_frame(128, 12, True, tail=b"\x00" * 5), 128, 12, 7), plain)


@pytest.mark.parametrize(
    ("encoded_width", "width"), [(64, 64), (64, 62), (64, 2), (128, 128), (128, 126), (128, 66)]
)
@pytest.mark.parametrize("depth", [0, 16])
def test_exact_and_padded_rows_keep_bayer_channels_and_row_groups(
    encoded_width: int, width: int, depth: int
) -> None:
    height = 12
    blocks = encoded_width // 64 * height
    image = struct.pack("<64H", *range(64)) * blocks if depth else b""
    bits = _metadata([depth] * blocks)
    # Distinct reference levels expose swapped Bayer channels, blocks or row groups.
    refs = _metadata([1000 + (block // 4) * 101 + (block % 4) * 17 for block in range(blocks)])
    offset = 16 + len(image)
    data = struct.pack("<4I", encoded_width, height, offset, offset + len(bits)) + image + bits + refs
    actual = _raw.decode(data, width, height, 7)
    y, x = np.mgrid[0:height, 0:width]
    reference = 1000 + (y // 4 * (encoded_width // 64) + x // 64) * 101 + ((y % 2) * 2 + x % 2) * 17
    residual = (y % 4 // 2) * 32 + x % 64 // 2 if depth else 0
    np.testing.assert_array_equal(actual, (reference + residual).astype(np.uint16))
