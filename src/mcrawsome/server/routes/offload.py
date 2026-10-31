"""Fast Copy from the interface: one background copy at a time, with its progress and results."""

from __future__ import annotations

import threading
from pathlib import Path

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field, field_validator

from mcrawsome.errors import McError
from mcrawsome.library.offload import CopyProgress, FileResult, Recording, find_recordings, offload_recordings
from mcrawsome.results import ErrorInfo


def _absolute(path: Path) -> Path:
    if not path.expanduser().is_absolute():
        raise ValueError(f"{path} is not a full path")
    return path.expanduser()


class ScanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sources: list[Path] = Field(min_length=1, max_length=64)

    @field_validator("sources")
    @classmethod
    def _full_paths(cls, sources: list[Path]) -> list[Path]:
        return [_absolute(source) for source in sources]


class OffloadRequest(ScanRequest):
    destination: Path
    verify: bool = True

    @field_validator("destination")
    @classmethod
    def _full_path(cls, destination: Path) -> Path:
        return _absolute(destination)


class ScanResult(BaseModel):
    recordings: list[Recording]
    bytes: int


class OffloadStatus(BaseModel):
    running: bool = False
    cancelling: bool = False
    destination: Path | None = None
    recordings: int = 0
    progress: CopyProgress | None = None
    results: list[FileResult] = Field(default_factory=list)
    error: ErrorInfo | None = None


class Offloader:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.thread: threading.Thread | None = None
        self.status = OffloadStatus()

    @property
    def running(self) -> bool:
        with self.lock:
            return self.status.running

    def view(self) -> OffloadStatus:
        with self.lock:
            return self.status.model_copy(deep=True)

    def start(self, request: OffloadRequest) -> OffloadStatus:
        recordings = find_recordings(request.sources)
        if not recordings:
            raise McError("No MotionCam recordings (.mcraw) were found there", code="input-missing")
        with self.lock:
            if self.status.running:
                raise McError("A copy is already running; wait for it or cancel it", code="engine-busy")
            self.stop.clear()
            self.status = OffloadStatus(
                running=True, destination=request.destination, recordings=len(recordings)
            )
            self.thread = threading.Thread(
                target=self._run, args=(recordings, request), name="offload", daemon=True
            )
            self.thread.start()
            return self.status.model_copy(deep=True)

    def _progress(self, progress: CopyProgress) -> None:
        with self.lock:
            self.status.progress = progress

    def _run(self, recordings: list[Recording], request: OffloadRequest) -> None:
        try:
            results = offload_recordings(
                recordings, request.destination, verify=request.verify, progress=self._progress,
                stop=self.stop.is_set,
            )  # fmt: skip
            with self.lock:
                self.status.results = results
        except (McError, OSError) as exc:
            code = exc.code if isinstance(exc, McError) else "output-unwritable"
            with self.lock:
                self.status.error = ErrorInfo(code=code, message=str(exc))
        finally:
            with self.lock:
                self.status.running = self.status.cancelling = False

    def cancel(self) -> OffloadStatus:
        """Stop after the block being copied; the copy keeps its .part file to continue later."""
        with self.lock:
            if self.status.running:
                self.stop.set()
                self.status.cancelling = True
            return self.status.model_copy(deep=True)

    def close(self) -> None:
        self.stop.set()
        if self.thread is not None:
            self.thread.join()


def offload_router(prefix: str, offloader: Offloader) -> APIRouter:
    router = APIRouter(prefix=prefix)

    @router.post("/offload/scan")
    def scan(request: ScanRequest) -> ScanResult:
        recordings = find_recordings(request.sources)
        return ScanResult(recordings=recordings, bytes=sum(r.size for r in recordings))

    @router.post("/offload", status_code=202)
    def start(request: OffloadRequest) -> OffloadStatus:
        return offloader.start(request)

    @router.get("/offload")
    def status() -> OffloadStatus:
        return offloader.view()

    @router.post("/offload/cancel")
    def cancel() -> OffloadStatus:
        return offloader.cancel()

    return router
