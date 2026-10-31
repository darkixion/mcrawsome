"""Bounded background metadata scans, shared by clients and keyed to source identity."""

from __future__ import annotations

import logging
import threading
from concurrent.futures import CancelledError, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from mcrawsome.analysis.capture_plot import CaptureAnalytics, measure_capture
from mcrawsome.pipeline.priority import background_priority

Status = Literal["waiting", "queued", "scanning", "ready", "failed"]
_MAX_ACTIVE = 4
_MAX_CACHED = 8


class CaptureScan(BaseModel):
    status: Status
    completed: int = 0
    total: int = 0
    report: CaptureAnalytics | None = None
    message: str | None = None


@dataclass
class _Scan:
    stop: threading.Event
    result: CaptureScan


class CaptureScanHost:
    def __init__(self) -> None:
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="capture-metadata", initializer=background_priority
        )
        self._lock = threading.Lock()
        self._scans: dict[tuple[Path, int, int, int, int], _Scan] = {}
        self._closed = False

    def request(self, path: Path, *, refresh: bool = False) -> CaptureScan:
        stat = path.stat()
        key = (path, stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
        with self._lock:
            if self._closed:
                return CaptureScan(status="failed", message="Metadata scanner is closed")
            existing = self._scans.get(key)
            if existing and (not refresh or existing.result.status in ("queued", "scanning")):
                return existing.result.model_copy(deep=False)
            # Superseded recordings do not keep consuming I/O after replacement.
            for old in [entry for entry in self._scans if entry[0] == path]:
                self._scans.pop(old).stop.set()
            active = sum(scan.result.status in ("queued", "scanning") for scan in self._scans.values())
            if active >= _MAX_ACTIVE:
                return CaptureScan(status="waiting", message="Waiting for the background metadata scanner")
            while len(self._scans) >= _MAX_CACHED:
                finished = next(
                    (
                        entry
                        for entry, scan in self._scans.items()
                        if scan.result.status in ("ready", "failed")
                    ),
                    None,
                )
                if finished is None:
                    break
                del self._scans[finished]
            scan = _Scan(threading.Event(), CaptureScan(status="queued"))
            self._scans[key] = scan
            self._executor.submit(self._run, path, scan)
            return scan.result.model_copy(deep=False)

    def _run(self, path: Path, scan: _Scan) -> None:
        def progress(completed: int, total: int) -> None:
            with self._lock:
                scan.result = CaptureScan(status="scanning", completed=completed, total=total)

        progress(0, 0)
        try:
            if scan.stop.is_set():
                raise CancelledError
            report = measure_capture(path, scan.stop, progress=progress)
            result = CaptureScan(status="ready", completed=report.frames, total=report.frames, report=report)
        except CancelledError:
            result = CaptureScan(status="failed", message="Metadata scan was cancelled")
        except Exception as exc:
            logging.getLogger(__name__).exception("Metadata scan failed for %s", path)
            result = CaptureScan(status="failed", message=str(exc) or "Could not read recording metadata")
        with self._lock:
            scan.result = result

    def close(self) -> None:
        with self._lock:
            self._closed = True
            for scan in self._scans.values():
                scan.stop.set()
        self._executor.shutdown(wait=True, cancel_futures=True)
