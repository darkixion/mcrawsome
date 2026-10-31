import threading
from concurrent.futures import CancelledError
from pathlib import Path

import numpy as np
import pytest

from mcrawsome.analysis.diagnostics import DiagnosticHost, bayer_histogram, timing_report
from mcrawsome.errors import McError
from mcrawsome.io.clip import Clip


@pytest.mark.parametrize("cfa", ["rggb", "bggr", "grbg", "gbrg"])
def test_raw_histogram_uses_bayer_sites_and_exact_recorded_thresholds(cfa: str) -> None:
    data = np.zeros((4, 8), np.uint16)
    for site in range(4):
        data[site // 2 :: 2, site % 2 :: 2] = [0, 64 + site, 1022, 1023]
    result = bayer_histogram(data, cfa, (64, 65, 66, 67), 1023)
    assert [plane.name for plane in result.planes] == ["R", "G (R row)", "G (B row)", "B"]
    for plane in result.planes:
        assert plane.samples == 8
        assert sum(plane.bins) == 8
        assert plane.at_or_below_black == 4
        assert plane.at_or_above_white == 2
        assert plane.minimum == 0
        assert plane.maximum == 1023
        assert len(plane.bins) == 256
    assert result.bin_edges[0] == 0
    assert result.bin_edges[-1] == 1024
    assert result.planes[0].black_level == 64 + cfa.index("r")
    expected_sites = {"rggb": [0, 1, 2, 3], "bggr": [3, 2, 1, 0], "grbg": [1, 0, 3, 2], "gbrg": [2, 3, 0, 1]}
    assert [plane.site for plane in result.planes] == expected_sites[cfa]


def test_histogram_is_bounded_and_cancellable() -> None:
    stopped = threading.Event()
    stopped.set()
    with pytest.raises(CancelledError):
        bayer_histogram(np.zeros((4, 4), np.uint16), "rggb", (0, 0, 0, 0), 1023, stopped)
    for size in [(3, 4), (4, 3), (0, 4)]:
        with pytest.raises(ValueError, match="even"):
            bayer_histogram(np.zeros(size, np.uint16), "rggb", (0, 0, 0, 0), 1023)
    with pytest.raises(ValueError, match="levels"):
        bayer_histogram(np.zeros((4, 4), np.uint16), "rggb", (64, 64, 64, 64), 64)


def test_timing_plot_retains_gaps_in_bounded_envelopes() -> None:
    intervals = np.full(2000, 40_000_000, np.int64)
    intervals[1111] = 120_000_000
    intervals[1200] = 1_000_000
    times = 8_000_000_000_000_000 + np.r_[np.int64(0), np.cumsum(intervals)]
    result = timing_report(times)
    assert len(result.buckets) <= 256
    assert sum(bucket.intervals for bucket in result.buckets) == 2000
    assert result.maximum_ms == 120
    assert result.minimum_ms == 1
    assert result.median_ms == 40
    assert max(bucket.maximum_ms for bucket in result.buckets) == 120
    assert result.dropped == 2
    assert result.duplicated == 1
    assert result.buckets[0].first_frame == 1
    assert result.buckets[-1].last_frame == 2000
    assert timing_report(np.array([100], np.int64)).median_ms is None
    with pytest.raises(ValueError, match="timestamps"):
        timing_report(np.array([100, 100, 100], np.int64))


def test_measurement_worker_has_no_queue_and_cancels_on_shutdown() -> None:
    entered, release = threading.Event(), threading.Event()
    host = DiagnosticHost()

    def wait(stop: threading.Event) -> int:
        entered.set()
        release.wait(5)
        if stop.is_set():
            raise CancelledError
        return 1

    future, stop = host.submit(wait)
    assert entered.wait(5)
    try:
        with pytest.raises(McError, match="already"):
            host.submit(wait)
        stop.set()
        release.set()
        with pytest.raises(CancelledError):
            future.result(timeout=5)
        result, _ = host.submit(lambda stop: 2)
        assert result.result(timeout=5) == 2
    finally:
        release.set()
        host.close()
    with pytest.raises(McError, match="closed"):
        host.submit(wait)


def test_histogram_reads_one_frame_without_development(tiny_raw: Path) -> None:
    from mcrawsome.analysis.diagnostics import measure_first_frame

    with Clip(tiny_raw) as clip:
        original = clip.raw_frame(0).data.copy()
    result = measure_first_frame(tiny_raw, threading.Event())
    assert result.frame_index == 0
    assert result.width == 128
    assert result.height == 64
    assert sum(plane.samples for plane in result.planes) == original.size


def test_invalid_size_is_refused_before_decode(tiny_raw: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from mcrawsome.analysis.diagnostics import measure_first_frame

    with Clip(tiny_raw) as clip:
        metadata = clip.frame_metadata(0)
    monkeypatch.setattr(
        Clip,
        "frame_metadata",
        lambda self, index: metadata.model_copy(update={"width": 65536, "height": 65536}),
    )
    monkeypatch.setattr(Clip, "raw_frame", lambda *args: pytest.fail("must not decode an oversized frame"))
    with pytest.raises(ValueError, match="64 Mi"):
        measure_first_frame(tiny_raw, threading.Event())


def test_over_white_codes_are_not_lost_and_fractional_thresholds_are_exact() -> None:
    data = np.array([[64, 65, 1022, 65535], [66, 67, 1023, 1024]], np.uint16)
    result = bayer_histogram(data, "rggb", (64.5, 65.5, 66.5, 67.5), 1022.5)
    assert result.bin_edges[-1] == 65536
    assert [plane.at_or_above_white for plane in result.planes] == [0, 1, 1, 1]
    assert all(sum(plane.bins) == plane.samples for plane in result.planes)


def test_worker_shutdown_signals_active_measurement() -> None:
    entered = threading.Event()
    host = DiagnosticHost()

    def measure(stop: threading.Event) -> None:
        entered.set()
        assert stop.wait(5)
        raise CancelledError

    future, _ = host.submit(measure)
    assert entered.wait(5)
    host.close()
    with pytest.raises(CancelledError):
        future.result(timeout=5)


def test_frames_needing_remosaic_are_measured_as_their_bayer_mosaic(tmp_path: Path) -> None:
    from quad_clips import quad_clip

    from mcrawsome.analysis.diagnostics import measure_first_frame

    clip = quad_clip(tmp_path / "quad.mcraw")
    histogram = measure_first_frame(clip, threading.Event())
    with Clip(clip) as opened:
        mosaic = opened.raw_frame(0).data
    expected = bayer_histogram(mosaic, opened.metadata.sensor_arrangement, (64.0,) * 4, 1023.0)
    assert [p.samples for p in histogram.planes] == [p.samples for p in expected.planes]
    assert [p.bins for p in histogram.planes] == [p.bins for p in expected.planes]
