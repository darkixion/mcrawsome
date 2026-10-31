import threading
from concurrent.futures import CancelledError

import numpy as np
import pytest

from mcrawsome.mount.readahead import FrameReadAhead, depth_for


@pytest.mark.parametrize(
    ("decoding", "intervals", "depth"),
    [
        ([0.009, 0.008], [0.0417, 0.0417], 1),  # an ordinary 4K frame at 24 fps
        ([0.042, 0.048], [0.0417, 0.0417], 2),  # a 4K Quad Bayer remosaic at 24 fps
        ([0.025, 0.027], [0.0417, 0.0417], 2),  # keeps up alone, but with nothing to spare for a slow frame
        ([0.002, 0.002], [0.001, 0.001], 1),  # quick decodes never need company, however fast the reads
        ([0.050], [0.0417, 0.0417], 1),  # too little history to judge
    ],
)
def test_two_frames_decode_at_once_only_when_one_thread_would_fall_behind(
    decoding: list[float], intervals: list[float], depth: int
) -> None:
    assert depth_for(decoding, intervals) == depth


def test_slow_decoding_keeps_two_frames_decoding_at_once_and_never_more(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    together = threading.Barrier(2, timeout=2)
    ahead: list[int] = []

    def prepare(index: int, stop: threading.Event):  # type: ignore[no-untyped-def]
        ahead.append(index)
        if index in (2, 3):
            together.wait()  # breaks, failing the read, unless both frames decode at the same time
        return np.full((2, 2), index, np.uint16)

    reader = FrameReadAhead(6, lambda i: np.full((2, 2), i, np.uint16), prepare)
    monkeypatch.setattr(reader, "depth", lambda: 2)
    try:
        reader.read(0)
        reader.read(1)
        assert len(reader._pending) == 2
        for index in range(2, 6):
            np.testing.assert_array_equal(reader.read(index), np.full((2, 2), index))
            assert len(reader._pending) <= 2
        assert sorted(ahead) == [2, 3, 4, 5], "a frame was decoded twice or past the end"
    finally:
        reader.close()


def test_a_seek_waits_for_neither_slow_frame_and_starts_none_until_they_finish(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started, release = threading.Semaphore(0), threading.Event()
    ahead: list[int] = []

    def prepare(index: int, stop: threading.Event):  # type: ignore[no-untyped-def]
        ahead.append(index)
        started.release()
        assert release.wait(2)
        return np.full((2, 2), index, np.uint16)

    reader = FrameReadAhead(100, lambda i: np.full((2, 2), i, np.uint16), prepare)
    monkeypatch.setattr(reader, "depth", lambda: 2)
    try:
        reader.read(0)
        reader.read(1)
        assert started.acquire(timeout=2)
        assert started.acquire(timeout=2)
        np.testing.assert_array_equal(reader.read(70), np.full((2, 2), 70))
        np.testing.assert_array_equal(reader.read(71), np.full((2, 2), 71))
        assert sorted(ahead) == [2, 3], "more speculative frames started while two were in flight"
    finally:
        release.set()
        reader.close()


def test_only_one_frame_is_prepared_after_forward_reads_and_consumed_without_redecode() -> None:
    demand: list[int] = []
    ahead: list[int] = []
    ready = threading.Event()

    def decode(index: int):  # type: ignore[no-untyped-def]
        demand.append(index)
        return np.full((2, 2), index, np.uint16)

    def prepare(index: int, stop: threading.Event):  # type: ignore[no-untyped-def]
        ahead.append(index)
        ready.set()
        return np.full((2, 2), index, np.uint16)

    reader = FrameReadAhead(3, decode, prepare)
    try:
        reader.read(0)
        assert ahead == []
        reader.read(1)
        assert ready.wait(2)
        assert ahead == [2], "read-ahead continued through the clip without another request"
        np.testing.assert_array_equal(reader.read(2), np.full((2, 2), 2))
        assert demand == [0, 1]
        assert ahead == [2], "read-ahead ran past the last recorded frame"
    finally:
        reader.close()


def test_seek_does_not_wait_for_or_queue_behind_an_unwanted_frame() -> None:
    started, release = threading.Event(), threading.Event()
    ahead: list[int] = []

    def prepare(index: int, stop: threading.Event):  # type: ignore[no-untyped-def]
        ahead.append(index)
        started.set()
        assert release.wait(2)
        return np.full((2, 2), index, np.uint16)

    reader = FrameReadAhead(100, lambda i: np.full((2, 2), i, np.uint16), prepare)
    try:
        reader.read(0)
        reader.read(1)
        assert started.wait(2)
        np.testing.assert_array_equal(reader.read(70), np.full((2, 2), 70))
        np.testing.assert_array_equal(reader.read(71), np.full((2, 2), 71))
        assert ahead == [2], "more speculative frames were queued while one was in flight"
    finally:
        release.set()
        reader.close()


def test_reverse_and_random_access_do_not_start_a_worker() -> None:
    def forbidden(index: int, stop: threading.Event):  # type: ignore[no-untyped-def]
        pytest.fail(f"speculative read on random access: {index}")

    reader = FrameReadAhead(100, lambda i: np.full((2, 2), i, np.uint16), forbidden)
    try:
        for index in (99, 98, 50, 3, 0):
            assert reader.read(index)[0, 0] == index
    finally:
        reader.close()


def test_speculative_errors_are_reported_only_when_the_affected_frame_is_requested() -> None:
    failed = threading.Event()

    def corrupt(index: int, stop: threading.Event):  # type: ignore[no-untyped-def]
        failed.set()
        raise ValueError("corrupt frame 2")

    reader = FrameReadAhead(100, lambda i: np.full((2, 2), i, np.uint16), corrupt)
    try:
        reader.read(0)
        reader.read(1)
        assert failed.wait(2)
        with pytest.raises(ValueError, match="corrupt frame 2"):
            reader.read(2)
        assert reader.read(70)[0, 0] == 70
    finally:
        reader.close()


def test_dropped_speculative_frames_are_decoded_again_when_read() -> None:
    demand: list[int] = []
    ready = threading.Event()

    def decode(index: int):  # type: ignore[no-untyped-def]
        demand.append(index)
        return np.full((2, 2), index, np.uint16)

    def prepare(index: int, stop: threading.Event):  # type: ignore[no-untyped-def]
        ready.set()
        return np.full((2, 2), index, np.uint16)

    reader = FrameReadAhead(10, decode, prepare)
    try:
        reader.read(0)
        reader.read(1)
        assert ready.wait(2)
        reader.drop()
        np.testing.assert_array_equal(reader.read(2), np.full((2, 2), 2))
        assert demand == [0, 1, 2], "a dropped frame was still served"
    finally:
        reader.close()


def test_close_cancels_waiting_speculation_joins_worker_and_releases_result() -> None:
    started, finished = threading.Event(), threading.Event()

    def prepare(index: int, stop: threading.Event):  # type: ignore[no-untyped-def]
        started.set()
        assert stop.wait(2)
        finished.set()
        raise CancelledError

    reader = FrameReadAhead(10, lambda i: np.full((2, 2), i, np.uint16), prepare)
    reader.read(0)
    reader.read(1)
    assert started.wait(2)
    reader.close()
    assert finished.is_set()
    reader.close()
    with pytest.raises(OSError, match="closed"):
        reader.read(2)
