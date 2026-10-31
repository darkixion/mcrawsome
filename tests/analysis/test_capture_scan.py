import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from mcrawsome.analysis import capture_scan
from mcrawsome.analysis.capture_plot import CaptureAnalytics
from mcrawsome.analysis.capture_scan import CaptureScanHost


def report() -> CaptureAnalytics:
    return CaptureAnalytics(frames=100, sampled_frames=100, unreadable_frames=0, timing=None, series=[])


def test_scan_is_nonblocking_reports_progress_and_reuses_stamped_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "a.mcraw"
    source.write_bytes(b"source")
    entered, release = threading.Event(), threading.Event()
    calls: list[Path] = []

    def measure(
        path: Path, stop: threading.Event, *, progress: Callable[[int, int], None]
    ) -> CaptureAnalytics:
        calls.append(path)
        progress(32, 100)
        entered.set()
        assert release.wait(5)
        return report()

    monkeypatch.setattr(capture_scan, "measure_capture", measure)
    host = CaptureScanHost()
    try:
        assert host.request(source).status == "queued"
        assert entered.wait(5)
        state = host.request(source)
        assert (state.status, state.completed, state.total) == ("scanning", 32, 100)
        assert calls == [source]
        release.set()
        host._executor.submit(lambda: None).result(timeout=5)
        assert host.request(source).status == "ready"
        assert len(calls) == 1
        source.write_bytes(b"replacement")
        host.request(source)
        host._executor.submit(lambda: None).result(timeout=5)
        assert len(calls) == 2
        host.request(source, refresh=True)
        host._executor.submit(lambda: None).result(timeout=5)
        assert len(calls) == 3
    finally:
        release.set()
        host.close()


def test_queue_is_bounded_and_another_clip_does_not_cancel_active_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered, release = threading.Event(), threading.Event()
    stopped = []

    def measure(
        path: Path, stop: threading.Event, *, progress: Callable[[int, int], None]
    ) -> CaptureAnalytics:
        entered.set()
        assert release.wait(5)
        stopped.append(stop.is_set())
        return report()

    monkeypatch.setattr(capture_scan, "measure_capture", measure)
    host = CaptureScanHost()
    try:
        paths = [tmp_path / f"{i}.mcraw" for i in range(5)]
        for source in paths:
            source.touch()
        for source in paths[:4]:
            assert host.request(source).status == "queued"
        assert entered.wait(5)
        assert host.request(paths[4]).status == "waiting"
        release.set()
        host._executor.submit(lambda: None).result(timeout=5)
        assert stopped == [False] * 4
        assert host.request(paths[4]).status == "queued"
    finally:
        release.set()
        host.close()


def test_failed_scan_is_visible_and_only_retries_explicitly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "bad.mcraw"
    source.touch()
    calls = []

    def broken(*args: Any, **kwargs: Any) -> CaptureAnalytics:
        calls.append(1)
        raise ValueError("Metadata is truncated")

    monkeypatch.setattr(capture_scan, "measure_capture", broken)
    host = CaptureScanHost()
    try:
        host.request(source)
        host._executor.submit(lambda: None).result(timeout=5)
        assert host.request(source).status == "failed"
        assert host.request(source).message == "Metadata is truncated"
        assert len(calls) == 1
        host.request(source, refresh=True)
        host._executor.submit(lambda: None).result(timeout=5)
        assert len(calls) == 2
    finally:
        host.close()
