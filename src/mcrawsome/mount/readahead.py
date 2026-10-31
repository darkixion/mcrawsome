"""Speculative frames after consecutive forward RAW reads: the next one, or the next two side by side when
decoding is slow; never a decode queue."""

from __future__ import annotations

import errno
import statistics
import threading
import time
from collections import deque
from collections.abc import Callable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any

import numpy as np
import numpy.typing as npt

Pixels = npt.NDArray[np.uint16]
DEPTH = 2  # speculative frames at most, each decoding on its own thread
SLOW_S = 0.010  # decoding faster than this never needs a second thread


def depth_for(decoding: Sequence[float], intervals: Sequence[float]) -> int:
    """Frames to decode ahead: two at once when a frame takes at least SLOW_S and more than half the time
    between the reader's frames, as one thread then falls behind or has nothing to spare for a slow frame
    (a 4K Quad Bayer remosaic decodes at about 23 fps alone on a laptop, 35 fps two at a time); else one."""
    if len(decoding) < 2 or len(intervals) < 2:
        return 1
    decode = statistics.median(decoding)
    return DEPTH if decode >= SLOW_S and decode > 0.5 * statistics.median(intervals) else 1


class FrameReadAhead:
    """The request thread owns the controller; only `ahead` runs on its private workers.

    A random seek decodes directly without waiting for unwanted speculative frames, and starts no new ones
    while those finish: at most DEPTH speculative results exist, outside the Sequence LRU, and no further
    work runs until a read.
    """

    def __init__(
        self,
        count: int,
        demand: Callable[[int], Pixels],
        ahead: Callable[[int, threading.Event], Pixels],
    ) -> None:
        self.count = count
        self.demand = demand
        self.ahead = ahead
        self.stop = threading.Event()
        self._executor: ThreadPoolExecutor | None = None
        self._pending: dict[int, Future[Pixels]] = {}
        self._previous: int | None = None
        self._read_at: float | None = None
        self._lock = threading.Lock()  # workers add decode times while the request thread reads them
        self._decoding: deque[float] = deque(maxlen=8)  # seconds each recent frame took to decode
        self._intervals: deque[float] = deque(maxlen=8)  # seconds between recent sequential reads

    def depth(self) -> int:
        with self._lock:
            return depth_for(list(self._decoding), list(self._intervals))

    def read(self, index: int) -> Pixels:
        if self.stop.is_set():
            raise OSError(errno.EBADF, "playback reader is closed")
        now = time.perf_counter()
        sequential = self._previous is not None and index == self._previous + 1
        if sequential and self._read_at is not None:
            with self._lock:
                self._intervals.append(now - self._read_at)
        self._read_at = now
        future = self._pending.pop(index, None)
        data = future.result() if future is not None else self._timed(self.demand, index)
        depth = self.depth() if sequential else 0
        for frame, waiting in list(self._pending.items()):
            # Off-path results go without surfacing their errors on another frame; running work finishes
            # on its own and counts until then.
            if not index < frame <= index + depth and (waiting.cancel() or waiting.done()):
                del self._pending[frame]
        for frame in range(index + 1, min(index + depth + 1, self.count)):
            if frame not in self._pending and len(self._pending) < depth:
                if self._executor is None:
                    self._executor = ThreadPoolExecutor(DEPTH, thread_name_prefix="mount-read-ahead")
                self._pending[frame] = self._executor.submit(self._timed, self.ahead, frame, self.stop)
        self._previous = index
        return data

    def _timed(self, decode: Callable[..., Pixels], *args: Any) -> Pixels:
        started = time.perf_counter()
        data = decode(*args)
        with self._lock:
            self._decoding.append(time.perf_counter() - started)
        return data

    def drop(self) -> None:
        """Forget the speculative frames, with the request thread held off: waiting work is cancelled and
        finished frames are freed; a decode already running finishes on its own and is discarded."""
        for waiting in self._pending.values():
            waiting.cancel()
        self._pending.clear()
        self._previous = None  # the next read starts a new run

    def close(self) -> None:
        self.stop.set()
        if self._executor is not None:
            self._executor.shutdown(wait=True, cancel_futures=True)
        self._pending.clear()
