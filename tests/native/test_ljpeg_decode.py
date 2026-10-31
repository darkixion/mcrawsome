import contextlib
import struct
import subprocess
import sys

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from mcrawsome import _raw


@pytest.mark.parametrize("bits", [8, 10, 12, 14, 16])
@pytest.mark.parametrize(("height", "width"), [(2, 2), (256, 256), (6, 18)])
def test_round_trip_with_the_encoder(bits: int, height: int, width: int) -> None:
    rng = np.random.default_rng(bits * 1000 + width)
    tile = rng.integers(0, 2**bits, (height, width), dtype=np.uint16)
    decoded = _raw.decode_ljpeg(_raw.encode_ljpeg(tile, bits))
    assert decoded.dtype == np.uint16
    np.testing.assert_array_equal(decoded, tile)  # two components of width / 2: the same row layout


def test_round_trip_of_extreme_differences() -> None:
    tile = np.zeros((4, 8), np.uint16)
    tile[:, 2::4] = 65535  # differences of 32768 and wrap-around in 16 bits
    tile[1::2, 1::2] = 32768
    np.testing.assert_array_equal(_raw.decode_ljpeg(_raw.encode_ljpeg(tile, 16)), tile)


def _bits(values: list[tuple[int, int]]) -> bytes:
    """Pack (value, length) codes MSB first, padded with ones, with 0xFF bytes stuffed."""
    acc, count, out = 0, 0, bytearray()
    for value, length in values:
        acc, count = (acc << length) | value, count + length
    pad = -count % 8
    acc, count = (acc << pad) | ((1 << pad) - 1), count + pad
    for i in range(count // 8):
        byte = (acc >> (count - 8 * (i + 1))) & 0xFF
        out.append(byte)
        if byte == 0xFF:
            out.append(0)
    return bytes(out)


def _segment(marker: int, body: bytes) -> bytes:
    return struct.pack(">BBH", 0xFF, marker, len(body) + 2) + body


def _diff_code(diff: int) -> list[tuple[int, int]]:
    """Category codes of a table with every category 0..15 at 5 bits: category s is code s."""
    if diff == 0:
        return [(0, 5)]
    size = abs(diff).bit_length()
    extra = diff if diff > 0 else diff + (1 << size) - 1
    return [(size, 5), (extra, size)]


def _one_component_stream(rows: list[list[int]], predictor: int, restart_rows: int) -> bytes:
    """A hand-built 8-bit, one-component stream with a restart marker every `restart_rows` rows."""
    height, width = len(rows), len(rows[0])
    dht = _segment(0xC4, bytes([0x00]) + bytes([0, 0, 0, 0, 16] + [0] * 11) + bytes(range(16)))
    sof = _segment(0xC3, struct.pack(">BHHB", 8, height, width, 1) + bytes([1, 0x11, 0]))
    dri = _segment(0xDD, struct.pack(">H", restart_rows * width))
    sos = _segment(0xDA, bytes([1, 1, 0x00, predictor, 0, 0]))
    data = b""
    for start in range(0, height, restart_rows):
        codes: list[tuple[int, int]] = []
        for y in range(start, min(start + restart_rows, height)):
            for x in range(width):
                if y == start:  # the first row of an interval: predictor 1, its first sample from 128
                    pred = 128 if x == 0 else rows[y][x - 1]
                elif x == 0:
                    pred = rows[y - 1][0]
                else:
                    ra, rb, rc = rows[y][x - 1], rows[y - 1][x], rows[y - 1][x - 1]
                    pred = {1: ra, 2: rb, 3: rc, 4: ra + rb - rc, 5: ra + ((rb - rc) >> 1),
                            6: rb + ((ra - rc) >> 1), 7: (ra + rb) // 2}[predictor]  # fmt: skip
                codes += _diff_code(rows[y][x] - pred)
        data += _bits(codes)
        if start + restart_rows < height:
            data += bytes([0xFF, 0xD0 + (start // restart_rows) % 8])
    return b"\xff\xd8" + dht + sof + dri + sos + data + b"\xff\xd9"


@pytest.mark.parametrize("predictor", [1, 2, 3, 4, 5, 6, 7])
def test_every_predictor_with_restarts(predictor: int) -> None:
    rng = np.random.default_rng(predictor)
    rows = rng.integers(0, 256, (6, 5)).tolist()
    decoded = _raw.decode_ljpeg(_one_component_stream(rows, predictor, restart_rows=2))
    np.testing.assert_array_equal(decoded, np.array(rows, np.uint16))


def _valid() -> bytes:
    tile = np.random.default_rng(3).integers(0, 4096, (16, 16), dtype=np.uint16)
    return _raw.encode_ljpeg(tile, 12)


@pytest.mark.parametrize(
    "data",
    [b"", b"\xff\xd8", _valid()[:40], _valid()[: len(_valid()) // 2], b"\x00" * 64, b"\xff\xd8\xff\xd9"],
)
def test_malformed_streams_are_refused(data: bytes) -> None:
    with pytest.raises(ValueError, match="lossless JPEG"):
        _raw.decode_ljpeg(data)


def test_a_frame_with_no_width_is_refused() -> None:
    data = bytearray(_valid())
    sof = data.index(b"\xff\xc3")
    data[sof + 7 : sof + 9] = b"\x00\x00"  # SOF3 width
    with pytest.raises(ValueError, match="lossless JPEG"):
        _raw.decode_ljpeg(bytes(data))


@pytest.mark.parametrize("selector", [0x40, 0xF0])
def test_scan_table_selectors_are_bounds_checked(selector: int) -> None:
    data = bytearray(_valid())
    sos = data.index(b"\xff\xda")
    data[sos + 6] = selector
    # Isolate the native call: a regression must fail the test without taking pytest down.
    script = (
        "from mcrawsome import _raw; import sys\n"
        "try: _raw.decode_ljpeg(bytes.fromhex(sys.argv[1]))\n"
        "except ValueError as exc:\n"
        " assert 'table number' in str(exc), str(exc)\n"
        "else: raise AssertionError('invalid selector accepted')\n"
    )
    result = subprocess.run([sys.executable, "-c", script, data.hex()], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_a_missing_end_marker_is_refused() -> None:
    with pytest.raises(ValueError, match="end-of-image"):
        _raw.decode_ljpeg(_valid()[:-2])


@settings(max_examples=300, deadline=None)
@given(st.lists(st.tuples(st.integers(0, 2000), st.integers(0, 255)), min_size=1, max_size=8))
def test_corrupted_streams_never_crash(changes: list[tuple[int, int]]) -> None:
    data = bytearray(_valid())
    for position, value in changes:
        data[position % len(data)] = value
    try:
        out = _raw.decode_ljpeg(bytes(data))
    except ValueError:
        return
    assert out.dtype == np.uint16


@settings(max_examples=100, deadline=None)
@given(st.binary(max_size=300))
def test_random_bytes_never_crash(data: bytes) -> None:
    with contextlib.suppress(ValueError):
        _raw.decode_ljpeg(b"\xff\xd8" + data)
