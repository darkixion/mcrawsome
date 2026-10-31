"""Explicit RAW measurements, isolated from preview startup and cancelled on disconnect."""

import asyncio
import threading
import time
from collections.abc import Callable, Generator
from concurrent.futures import CancelledError, Future
from functools import partial
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from mcrawsome.analysis.capture_checks import CaptureChecks, capture_checks
from mcrawsome.analysis.capture_plot import (
    CaptureAnalytics,
    ShadingMap,
    capture_csv,
    measure_capture,
    measure_shading,
)
from mcrawsome.analysis.capture_scan import CaptureScan, CaptureScanHost
from mcrawsome.analysis.clipping import ClippingImage, Mode, measure_clipping
from mcrawsome.analysis.diagnostics import (
    DiagnosticHost,
    RawHistogram,
    TimingDiagnostics,
    measure_first_frame,
    measure_timing,
)
from mcrawsome.analysis.gyro_plot import GyroPlot, measure_gyro
from mcrawsome.analysis.motion_insights import MotionReport, motion_report
from mcrawsome.analysis.preview_overlay import PreviewOverlays, measure_overlays
from mcrawsome.errors import McError
from mcrawsome.library.scan import ClipSummary
from mcrawsome.recipe.schema import Recipe
from mcrawsome.resolve.kept_frame import KeptFrame, kept_frame
from mcrawsome.server.routes.errors import ERROR_RESPONSES

WAIT_S = 30.0  # how long a diagnostic waits for the worker before it is refused as busy


class OverlayRequest(BaseModel):
    recipe: Recipe


class KeptFrameRequest(BaseModel):
    recipe: Recipe
    # A first look, planned without image-assisted refinement (most of the time), before the plan a send uses.
    quick: bool = False


class ClippingRequest(BaseModel):
    recipe: Recipe
    index: int = Field(ge=0)
    mode: Mode = "any"
    width: int = Field(default=1280, ge=16, le=1920)


def diagnostics_router(
    prefix: str,
    find: Callable[[str], ClipSummary],
    host: DiagnosticHost,
    scans: CaptureScanHost,
    planner: DiagnosticHost | None = None,
) -> APIRouter:
    """`planner` plans the kept frame of a stabilised timeline, which takes seconds: on its own worker,
    it never holds up the other diagnostics."""
    router = APIRouter(prefix=prefix)

    async def start[T](
        request: Request, work: Callable[[threading.Event], T], on: DiagnosticHost
    ) -> tuple[Future[T], threading.Event]:
        """Start `work` on the worker `on`. While another diagnostic holds it (often one whose client just
        moved to another clip and which is stopping), wait rather than refuse."""
        deadline = time.monotonic() + WAIT_S
        while True:
            try:
                return on.submit(work)
            except McError as exc:
                if exc.code != "engine-busy" or on.closed or time.monotonic() > deadline:
                    raise
            if await request.is_disconnected():
                raise McError("RAW diagnostic cancelled", code="paused")
            await asyncio.sleep(0.05)

    async def measure[T](
        identity: str,
        request: Request,
        work: Callable[..., T],
        *,
        raw_only: bool = True,
        on: DiagnosticHost = host,
    ) -> T:
        clip = await asyncio.to_thread(find, identity)  # a rescan, when one is needed, keeps the loop free
        if (raw_only and clip.kind != "raw") or clip.status != "ready":
            raise McError("Recorded RAW diagnostics need a ready RAW clip", code="recipe-invalid")
        future, stop = await start(request, partial(work, clip.path), on)
        try:
            while not future.done():
                if await request.is_disconnected():
                    raise McError("RAW diagnostic cancelled", code="paused")
                await asyncio.sleep(0.05)
            return future.result()
        except CancelledError:
            raise McError("RAW diagnostic cancelled", code="paused") from None
        except (ValueError, OSError, RuntimeError) as exc:
            raise McError(f"Cannot measure {clip.name}: {exc}", code="input-unreadable") from None
        finally:
            stop.set()

    @router.post("/clips/{clip_id}/sensor-clipping", responses=ERROR_RESPONSES)
    async def sensor_clipping(clip_id: str, body: ClippingRequest, request: Request) -> ClippingImage:
        return await measure(
            clip_id,
            request,
            lambda path, stop: measure_clipping(path, body.recipe, body.index, body.mode, body.width, stop),
        )

    @router.get("/clips/{clip_id}/analytics-scan", responses=ERROR_RESPONSES)
    async def analytics_scan(clip_id: str, refresh: bool = False) -> CaptureScan:
        clip = await asyncio.to_thread(find, clip_id)
        if clip.status != "ready":
            raise McError("Metadata scan needs a ready clip", code="recipe-invalid")
        return scans.request(clip.path, refresh=refresh)

    @router.get("/clips/{clip_id}/analytics", responses=ERROR_RESPONSES)
    async def analytics(clip_id: str, request: Request) -> CaptureAnalytics:
        return await measure(clip_id, request, measure_capture, raw_only=False)

    @router.get("/clips/{clip_id}/shading-map", responses=ERROR_RESPONSES)
    async def shading(clip_id: str, frame: int, request: Request) -> ShadingMap:
        return await measure(clip_id, request, lambda path, stop: measure_shading(path, frame, stop))

    @router.get("/clips/{clip_id}/capture.csv", responses=ERROR_RESPONSES)
    async def csv_report(clip_id: str) -> StreamingResponse:
        clip = find(clip_id)
        if clip.kind != "raw" or clip.status != "ready":
            raise McError("Per-frame RAW metadata export needs a ready RAW clip", code="recipe-invalid")
        stop = host.reserve()

        def rows() -> Generator[str, None, None]:
            try:
                yield from capture_csv(clip.path, stop)
            finally:
                host.release(stop)

        iterator = rows()

        def cleanup() -> None:
            stop.set()
            iterator.close()
            host.release(stop)

        return StreamingResponse(
            iterator,
            background=BackgroundTask(cleanup),
            media_type="text/csv",
            headers={
                "Content-Disposition": 'attachment; filename="mcrawsome-capture.csv"',
                "Cache-Control": "no-store",
            },
        )

    @router.post("/clips/{clip_id}/preview-overlays", responses=ERROR_RESPONSES)
    async def overlays(clip_id: str, body: OverlayRequest, request: Request) -> PreviewOverlays:
        def work(path: Path, stop: threading.Event) -> PreviewOverlays:
            return measure_overlays(path, body.recipe, stop)

        return await measure(clip_id, request, work, raw_only=False)

    @router.post("/clips/{clip_id}/kept-frame", responses=ERROR_RESPONSES)
    async def kept(clip_id: str, body: KeptFrameRequest, request: Request) -> KeptFrame:
        """Where a stabilised timeline's kept frame lies on each recorded frame with these settings,
        planned as a send plans it; given up when the page stops waiting."""
        return await measure(
            clip_id,
            request,
            lambda path, stop: kept_frame(path, body.recipe, stop, quick=body.quick),
            on=planner if planner is not None else host,
        )

    @router.get("/clips/{clip_id}/timing", responses=ERROR_RESPONSES)
    async def timing(clip_id: str, request: Request) -> TimingDiagnostics:
        return await measure(clip_id, request, measure_timing)

    @router.get("/clips/{clip_id}/raw-histogram", responses=ERROR_RESPONSES)
    async def histogram(clip_id: str, request: Request) -> RawHistogram:
        return await measure(clip_id, request, measure_first_frame)

    @router.get("/clips/{clip_id}/gyro", responses=ERROR_RESPONSES)
    async def gyro(clip_id: str, request: Request) -> GyroPlot:
        return await measure(clip_id, request, measure_gyro, raw_only=False)

    @router.get("/clips/{clip_id}/checks", responses=ERROR_RESPONSES)
    async def checks(clip_id: str, request: Request) -> CaptureChecks:
        """Capture warnings from every frame's metadata and the sound; no picture is decoded."""
        return await measure(clip_id, request, capture_checks, raw_only=False)

    @router.get("/clips/{clip_id}/motion", responses=ERROR_RESPONSES)
    async def motion(clip_id: str, request: Request) -> MotionReport:
        """Movement, shake blur and a suggested trim from the gyro log; no picture is decoded."""
        return await measure(clip_id, request, motion_report, raw_only=False)

    return router
