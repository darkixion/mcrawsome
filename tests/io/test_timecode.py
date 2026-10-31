"""Time-of-day timecode from the phone's clock, with known answers."""

import struct
from datetime import UTC, datetime

import numpy as np
import pytest

from mcrawsome.io.timecode import Timecode, bext, capture_start, local_zone, valid_zone

# 4 October 2026 15:41:23.080 UTC is 16:41:23.080 in London (British Summer Time, which ends on 25 October).
EPOCH = datetime(2026, 10, 4, 15, 41, 23, 80_000, tzinfo=UTC).timestamp()


def test_the_first_frame_is_linked_through_the_smallest_receive_latency_and_the_sync_offset() -> None:
    stamps = np.arange(5, dtype=np.int64) * 41_666_667 + 7_000_000_000  # sensor clock
    latency_ms = np.array([30.0, 12.0, 25.0, 40.0, 18.0])
    received = (EPOCH * 1000 + (stamps - stamps[0]) / 1e6 + latency_ms).round()
    assert capture_start(stamps, received, None) == pytest.approx(EPOCH + 0.012, abs=1e-3)
    assert capture_start(stamps, received, -105) == pytest.approx(EPOCH + 0.012 - 0.105, abs=1e-3)
    with pytest.raises(ValueError, match="receive times"):
        capture_start(stamps, np.zeros(5), None)


def test_timecode_counts_frames_from_local_midnight() -> None:
    code = Timecode.at(EPOCH, 24.002, "Europe/London")
    assert code.start_frames == (16 * 3600 + 41 * 60 + 23) * 24 + 1
    assert code.text() == "16:41:23:01"
    assert code.text(23) == "16:41:24:00"
    assert code.address(0) == bytes([0x01, 0x23, 0x41, 0x16, 0, 0, 0, 0])
    assert Timecode.at(EPOCH, 24, "UTC").text() == "15:41:23:01"
    assert code.time_reference(48000) == code.start_frames * 2000


def test_timecode_wraps_at_midnight_and_counts_pairs_above_30_fps() -> None:
    late = datetime(2026, 10, 25, 23, 59, 59, 960_000, tzinfo=UTC).timestamp()
    code = Timecode.at(late, 25, "UTC")
    assert code.text() == "23:59:59:24"
    assert code.text(1) == "00:00:00:00"
    sixty = Timecode.at(EPOCH, 59.94, "UTC")
    assert (sixty.per_second, sixty.group) == (30, 2)
    first, second = sixty.address(0), sixty.address(1)
    assert first[0] == second[0] or first[1] & 0x80 != second[1] & 0x80
    assert {first[1] & 0x80, second[1] & 0x80} == {0, 0x80}


def test_unknown_zones_are_refused_and_the_local_zone_is_valid() -> None:
    with pytest.raises(ValueError, match="time zone"):
        Timecode.at(EPOCH, 24, "Mars/Olympus_Mons")
    assert valid_zone(local_zone())


def test_the_bext_chunk_carries_the_time_reference() -> None:
    code = Timecode.at(EPOCH, 24, "Europe/London")
    chunk = bext(code.time_reference(48000), code.start_local, "clip")
    assert chunk[:4] == b"bext"
    assert struct.unpack_from("<I", chunk, 4)[0] == 602
    assert chunk[8 + 320 : 8 + 330] == b"2026-10-04"
    assert chunk[8 + 330 : 8 + 338] == b"16:41:23"
    assert struct.unpack_from("<QH", chunk, 8 + 338) == (code.time_reference(48000), 1)
