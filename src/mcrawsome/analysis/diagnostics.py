"""Bounded, on-demand measurements of recorded RAW values and frame timestamps."""

from __future__ import annotations

import math
import threading
from collections.abc import Callable
from concurrent.futures import CancelledError, Future, ThreadPoolExecutor
from pathlib import Path

import numpy as np
import numpy.typing as npt
from pydantic import BaseModel

from mcrawsome.errors import McError
from mcrawsome.io.clip import Clip
from mcrawsome.io.describe import dropped_frames, duplicated_frames
from mcrawsome.pipeline.priority import background_priority

MAX_PIXELS = 64 * 1024 * 1024
BINS = 256


def _check(stop: threading.Event | None) -> None:
    if stop is not None and stop.is_set():
        raise CancelledError


class HistogramPlane(BaseModel):
    name: str
    site: int  # row-major index of the physical Bayer site
    black_level: float
    white_level: float
    samples: int
    minimum: int
    maximum: int
    at_or_below_black: int
    at_or_above_white: int
    bins: list[int]


class RawHistogram(BaseModel):
    frame_index: int = 0
    timestamp_ns: str = ""  # decimal text retains nanoseconds beyond JavaScript's integer precision
    width: int
    height: int
    cfa: str
    bin_edges: list[int]
    planes: list[HistogramPlane]


def bayer_histogram(
    data: npt.NDArray[np.uint16],
    cfa: str,
    blacks: tuple[float, float, float, float],
    white: float,
    stop: threading.Event | None = None,
) -> RawHistogram:
    """Every photosite counted once, before black subtraction, gains, white balance or development.

    Raw-code accumulators have a fixed 2 MiB total size. Temporary count inputs contain at most
    262144 samples. Display bins cover a power-of-two range, including any above-white codes.
    """
    _check(stop)
    if data.ndim != 2 or not all(size > 0 and size % 2 == 0 for size in data.shape):
        raise ValueError("RAW histogram needs positive even frame dimensions")
    if data.dtype != np.uint16 or data.size > MAX_PIXELS:
        raise ValueError("RAW histogram needs uint16 data with at most 64 Mi photosites")
    if cfa not in ("rggb", "bggr", "grbg", "gbrg"):
        raise ValueError("RAW histogram needs a Bayer pattern")
    if (
        not math.isfinite(white)
        or white > 65535
        or any(not math.isfinite(black) or black < 0 or black >= white for black in blacks)
    ):
        raise ValueError("Invalid recorded black/white levels")
    counts = []
    for site in range(4):
        plane = data[site // 2 :: 2, site % 2 :: 2]
        histogram = np.zeros(65536, np.int64)
        rows = max(1, 262144 // plane.shape[1])
        for top in range(0, plane.shape[0], rows):
            _check(stop)
            histogram += np.bincount(plane[top : top + rows].ravel(), minlength=65536)
        counts.append(histogram)
    _check(stop)
    domain = max(BINS, 1 << max(math.ceil(white), int(data.max())).bit_length())
    width = domain // BINS
    red, blue = cfa.index("r"), cfa.index("b")
    green = [i for i, value in enumerate(cfa) if value == "g"]
    green.sort(key=lambda i: i // 2 != red // 2)
    order = [red, *green, blue]
    names = ("R", "G (R row)", "G (B row)", "B")
    result = []
    for name, site in zip(names, order, strict=True):
        values = counts[site]
        populated = np.flatnonzero(values)
        result.append(HistogramPlane(
            name=name, site=site, black_level=blacks[site], white_level=white,
            samples=int(values.sum()), minimum=int(populated[0]), maximum=int(populated[-1]),
            at_or_below_black=int(values[: math.floor(blacks[site]) + 1].sum()),
            at_or_above_white=int(values[math.ceil(white) :].sum()),
            bins=values[:domain].reshape(BINS, width).sum(axis=1).tolist(),
        ))  # fmt: skip
    return RawHistogram(
        width=data.shape[1], height=data.shape[0], cfa=cfa,
        bin_edges=list(range(0, domain + 1, width)), planes=result,
    )  # fmt: skip


def measure_first_frame(path: Path, stop: threading.Event) -> RawHistogram:
    _check(stop)
    with Clip(path) as clip:
        frame = clip.frame_metadata(0)
        if (
            frame.width <= 0
            or frame.height <= 0
            or frame.width % 2
            or frame.height % 2
            or frame.width * frame.height > MAX_PIXELS
        ):
            raise ValueError("First RAW frame needs even dimensions with at most 64 Mi photosites")
        _check(stop)
        raw = clip.raw_frame(0)
        result = bayer_histogram(
            raw.data, clip.metadata.sensor_arrangement, frame.black_level, frame.white_level, stop
        )
        return result.model_copy(update={"timestamp_ns": str(raw.timestamp_ns)})


class IntervalBucket(BaseModel):
    time_s: float | None = None
    first_frame: int  # zero-based recorded frame at which the first interval ends
    last_frame: int
    intervals: int
    minimum_ms: float
    maximum_ms: float
    mean_ms: float


class TimingDiagnostics(BaseModel):
    frames: int
    intervals: int
    median_ms: float | None
    minimum_ms: float | None
    maximum_ms: float | None
    dropped: int
    duplicated: int
    nonpositive: int
    buckets: list[IntervalBucket]
    mean_ms: float | None = None
    deviation_us: float | None = None


def timing_report(times: npt.NDArray[np.int64], stop: threading.Event | None = None) -> TimingDiagnostics:
    _check(stop)
    if len(times) < 2:
        return TimingDiagnostics(
            frames=len(times),
            intervals=0,
            median_ms=None,
            minimum_ms=None,
            maximum_ms=None,
            dropped=0,
            duplicated=0,
            nonpositive=0,
            buckets=[],
        )
    intervals = np.diff(times).astype(np.float64) / 1e6
    median = float(np.median(intervals))
    if median <= 0:
        raise ValueError("Recorded frame timestamps have no positive median interval")
    buckets = []
    step = math.ceil(len(intervals) / 256)
    for start in range(0, len(intervals), step):
        _check(stop)
        block = intervals[start : start + step]
        buckets.append(IntervalBucket(
            first_frame=start + 1, last_frame=start + len(block), intervals=len(block),
            time_s=float(times[(start + 1 + start + len(block)) // 2] - times[0]) / 1e9,
            minimum_ms=float(block.min()), maximum_ms=float(block.max()), mean_ms=float(block.mean()),
        ))  # fmt: skip
    return TimingDiagnostics(
        frames=len(times), intervals=len(intervals), median_ms=median,
        minimum_ms=float(intervals.min()), maximum_ms=float(intervals.max()),
        dropped=dropped_frames(times), duplicated=duplicated_frames(times),
        nonpositive=int((intervals <= 0).sum()), buckets=buckets,
        mean_ms=float(intervals.mean()), deviation_us=float(intervals.std() * 1000),
    )  # fmt: skip


def measure_timing(path: Path, stop: threading.Event) -> TimingDiagnostics:
    _check(stop)
    with Clip(path) as clip:
        return timing_report(clip.timestamps_ns, stop)


class DiagnosticHost:
    """One background diagnostic operation, with no pending queue or retained frame buffers."""

    def __init__(self) -> None:
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="raw-diagnostics", initializer=background_priority
        )
        self._lock = threading.Lock()
        self._stop: threading.Event | None = None
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    def reserve(self) -> threading.Event:
        """Reserve the shared metadata worker, including streamed reports."""
        with self._lock:
            if self._closed:
                raise McError("RAW diagnostics are closed", code="engine-busy")
            if self._stop is not None:
                raise McError("A RAW diagnostic is already running; wait or cancel it", code="engine-busy")
            stop = self._stop = threading.Event()
            return stop

    def release(self, stop: threading.Event) -> None:
        stop.set()
        with self._lock:
            if self._stop is stop:
                self._stop = None

    def submit[T](self, work: Callable[[threading.Event], T]) -> tuple[Future[T], threading.Event]:
        stop = self.reserve()
        with self._lock:

            def run() -> T:
                try:
                    return work(stop)
                finally:
                    with self._lock:
                        self._stop = None

            return self._executor.submit(run), stop

    def close(self) -> None:
        with self._lock:
            self._closed = True
            if self._stop is not None:
                self._stop.set()
        self._executor.shutdown(wait=True, cancel_futures=True)
