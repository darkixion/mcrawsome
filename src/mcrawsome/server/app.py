"""Local HTTP API for recording libraries, previews and Resolve media mounts."""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import threading
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from types import FrameType
from typing import Any

import uvicorn
from fastapi import APIRouter, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel
from starlette.datastructures import Headers
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Receive, Scope, Send

from mcrawsome import __version__
from mcrawsome.analysis.capture_scan import CaptureScanHost
from mcrawsome.analysis.diagnostics import DiagnosticHost
from mcrawsome.build import build_id
from mcrawsome.doctor import Check, run_checks
from mcrawsome.errors import ErrorCode, McError
from mcrawsome.library.scan import ClipSummary, Library, inside
from mcrawsome.library.sessions import Sessions
from mcrawsome.library.watch import LibraryWatcher
from mcrawsome.mount.manager import MountManager, availability
from mcrawsome.packaging import packaging_dir
from mcrawsome.results import ErrorInfo
from mcrawsome.server.config import ServerConfig, check_exposure, is_loopback
from mcrawsome.server.progress import ProgressHub
from mcrawsome.server.routes import cache as cache_routes
from mcrawsome.server.routes import settings as settings_routes
from mcrawsome.server.routes.annotations import annotations_router
from mcrawsome.server.routes.camera_profiles import camera_profiles_router
from mcrawsome.server.routes.cameras import cameras_router
from mcrawsome.server.routes.clips import AnalysisHost, clips_router
from mcrawsome.server.routes.diagnostics import diagnostics_router
from mcrawsome.server.routes.library import find_clip, library_router
from mcrawsome.server.routes.mounts import mounts_router
from mcrawsome.server.routes.offload import Offloader, offload_router
from mcrawsome.server.routes.opened import opened_router
from mcrawsome.server.routes.plan import colour_router
from mcrawsome.server.routes.preview import PreviewHost, preview_router
from mcrawsome.server.routes.reports import reports_router
from mcrawsome.server.routes.reset import reset_router
from mcrawsome.server.routes.sessions import sessions_router
from mcrawsome.server.routes.support import support_router
from mcrawsome.server.routes.vignette import vignette_router
from mcrawsome.server.settings import InterfaceSettings, load_settings
from mcrawsome.server.static import add_interface

API = "/api/v1"
EVENT_POLL_S = 0.5  # library, analysis, media-use and send-stage updates
STATUS: dict[str, int] = {
    "recipe-invalid": 422,
    "recipe-changed": 409,
    "engine-busy": 409,
    "input-missing": 404,
    "input-unreadable": 422,
    "clip-missing": 404,
    "path-not-allowed": 403,
    "client-not-allowed": 403,
    "origin-not-allowed": 403,
    "output-exists": 409,
    "output-unwritable": 422,
    "paused": 409,
    "disk-full": 507,
    "host-not-allowed": 400,
    "unauthorised": 401,
}

log = logging.getLogger(__name__)


class Health(BaseModel):
    version: str
    build: str  # mcrawsome.build.build_id: the version alone does not tell two builds apart
    pid: int  # this server's process id, so a window can tell the engine it started
    active_mounts: int = 0


class CheckList(BaseModel):
    checks: list[Check]  # the shape of `doctor --json`


class Licence(BaseModel):
    file: str
    text: str


class About(BaseModel):
    version: str
    checks: list[Check]
    licences: list[str]  # file names in packaging/LICENCES; each text is GET /about/licences/{file}


def _error(code: ErrorCode, message: str, field: str | None = None) -> JSONResponse:
    """The error body: `{"error": {"code", "message"}}`, with `field` naming the request's field at fault
    when one is."""
    error: dict[str, Any] = ErrorInfo(code=code, message=message).model_dump()
    if field is not None:
        error["field"] = field
    return JSONResponse({"error": error}, status_code=STATUS.get(code, 500))


class Guard:
    """Refuses a request or WebSocket whose Host, Origin or token is not this server's, before it reaches
    the API."""

    def __init__(self, app: ASGIApp, config: ServerConfig) -> None:
        self.app = app
        self.token = config.token
        self.hosts: set[str] | None = None  # any Host, off loopback: the server may be reached by any name
        if is_loopback(config.host):
            self.hosts = {f"{name}:{config.port}" for name in ("127.0.0.1", "localhost", "[::1]")}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in ("http", "websocket"):  # a refused WebSocket gets the HTTP answer (403 and so on)
            refusal = self._refusal(Headers(scope=scope))
            if refusal is not None:
                if scope["type"] == "websocket" and "websocket.http.response" not in scope.get(
                    "extensions", {}
                ):
                    await send({"type": "websocket.close", "code": 1008})  # 403 at the handshake
                else:
                    await refusal(scope, receive, send)
                return
        await self.app(scope, receive, send)

    def _refusal(self, headers: Headers) -> JSONResponse | None:
        host = headers.get("host", "").lower()
        if self.hosts is not None and host not in self.hosts:
            names = ", ".join(sorted(self.hosts))
            return _error("host-not-allowed", f"the Host header must name this machine ({names})")
        origin = headers.get("origin")
        if origin is not None and origin.lower() not in (f"http://{host}", f"https://{host}"):
            return _error("origin-not-allowed", f"requests from {origin} are not allowed")
        if self.token is not None:
            scheme, _, given = headers.get("authorization", "").partition(" ")
            if scheme.lower() != "bearer" or not hmac.compare_digest(given.encode(), self.token.encode()):
                refusal = _error("unauthorised", "this server needs the header 'Authorization: Bearer TOKEN'")
                refusal.headers["WWW-Authenticate"] = "Bearer"
                return refusal
        return None


def _allowed(path: Path, library: list[Path]) -> Path:
    """The path with symlinks resolved, when it lies in a library folder."""
    resolved = inside(path, library)
    if resolved is None:
        folders = ", ".join(str(root) for root in library)
        raise McError(f"{path} is outside the library folders ({folders})", code="path-not-allowed")
    return resolved


async def _events(
    library: LibraryWatcher,
    request: Request,
    stopping: threading.Event,
    analysis: AnalysisHost | None = None,
    mounts: MountManager | None = None,
    previews: PreviewHost | None = None,
    gpu: bool = True,
    progress: ProgressHub | None = None,
) -> AsyncGenerator[str, None]:
    changes = library.subscribe()
    analyses = analysis.subscribe() if analysis is not None else None
    stages = progress.subscribe() if progress is not None else None
    seen_usage: dict[str, bool | None] | None = None
    if previews is not None:  # a window is open: its first preview should not wait for a process
        previews.window_opened(gpu)
    try:
        yield ": connected\n\n"
        while not stopping.is_set() and not await request.is_disconnected():
            await asyncio.sleep(EVENT_POLL_S)
            if mounts is not None:
                current_usage = await asyncio.to_thread(mounts.usage_snapshot)
                if current_usage != seen_usage:
                    seen_usage = current_usage
                    yield f"event: media-use\ndata: {json.dumps(current_usage)}\n\n"
            while not changes.empty():
                yield f"event: library\ndata: {changes.get_nowait().model_dump_json()}\n\n"
            if analyses is not None:
                while not analyses.empty():
                    yield f"event: analysis\ndata: {json.dumps(analyses.get_nowait())}\n\n"
            if stages is not None:
                while not stages.empty():
                    yield f"event: send\ndata: {json.dumps(stages.get_nowait())}\n\n"
    finally:
        library.unsubscribe(changes)
        if progress is not None and stages is not None:
            progress.unsubscribe(stages)
        if analysis is not None and analyses is not None:
            analysis.unsubscribe(analyses)
        if previews is not None:
            previews.window_closed()


def create_app(config: ServerConfig) -> FastAPI:
    """Serve media operations without an export queue or worker."""
    check_exposure(config)
    roots = config.library()
    stopping = threading.Event()  # set by serve() on Ctrl+C: open event streams end
    previews = PreviewHost()
    mounts = MountManager()
    progress = ProgressHub()  # the stages of Resolve sends, for the page waiting on them

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        stop = threading.Event()
        threads = [threading.Thread(target=clips.run, args=(stop,), name="library", daemon=True)]
        if _app.state.settings.remount_on_start and availability()[0] and (offline := mounts.sent_offline()):
            # Not joined on shutdown: a stop between mounts leaves the rest for the next start.
            threading.Thread(target=mounts.restore, args=(offline,), name="remount", daemon=True).start()
        for thread in threads:
            thread.start()
        try:
            yield
        finally:
            stop.set()
            await asyncio.to_thread(offloader.close)
            await asyncio.to_thread(previews.close)
            await asyncio.to_thread(analysis.close)
            await asyncio.to_thread(diagnostics.close)
            await asyncio.to_thread(planner.close)
            await asyncio.to_thread(capture_scans.close)
            for thread in threads:
                await asyncio.to_thread(thread.join)

    app = FastAPI(
        title="MCRAWsome",
        version=__version__,
        openapi_url=f"{API}/openapi.json",
        docs_url=None,  # the Swagger and ReDoc pages load scripts from a CDN
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.config = config
    app.state.settings, app.state.settings_problem = load_settings()  # both replaced by PUT /settings
    app.state.stopping = stopping
    app.state.previews = previews
    app.add_middleware(Guard, config=config)

    @app.exception_handler(McError)
    async def mc_error(_request: Request, exc: McError) -> JSONResponse:
        return _error(exc.code, exc.message)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_request: Request, exc: RequestValidationError) -> JSONResponse:
        problems = [
            f"{'.'.join(str(part) for part in error['loc'][1:]) or 'body'}: "
            + error["msg"].removeprefix("Value error, ")  # a validator's own message, as written
            for error in exc.errors()
        ]
        fields = [
            error["loc"][1] for error in exc.errors() if len(error["loc"]) > 1 and error["loc"][0] == "body"
        ]
        return _error("recipe-invalid", "; ".join(problems), str(fields[0]) if fields else None)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code: ErrorCode = "input-missing" if exc.status_code == 404 else "input-unreadable"
        message = "no such route" if exc.status_code == 404 else str(exc.detail)
        return JSONResponse(
            {"error": ErrorInfo(code=code, message=message).model_dump()},
            status_code=exc.status_code,
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(Exception)
    async def internal_error(_request: Request, exc: Exception) -> JSONResponse:
        log.error("unhandled error in the API", exc_info=exc)
        return _error("internal-error", "the server failed on this request; see its log")

    sessions = Sessions()
    opened = sessions.opened

    def library() -> list[Path]:
        settings: InterfaceSettings = app.state.settings
        return [*roots, *settings.roots(), *opened.permitted()]

    def folders() -> list[Path]:
        settings: InterfaceSettings = app.state.settings
        return list(settings.folders)

    offloader = Offloader()

    clips = LibraryWatcher(Library(), folders, library)

    def find(clip_id: str) -> ClipSummary:
        known = opened.library.find(clip_id)  # an opened clip needs no rescan of the folders to be found
        if known is not None and clips.library.find(clip_id) is None and known.path.is_file():
            _allowed(known.path, library())
            return known
        try:
            return find_clip(clip_id, lambda path: _allowed(path, library()), clips)
        except McError as exc:
            if exc.code != "clip-missing":
                raise
            opened.refresh()
            found = opened.library.find(clip_id)
            if found is None:
                raise exc
            _allowed(found.path, library())
            return found

    analysis = AnalysisHost(find, lambda: app.state.settings.workers or config.workers)
    app.state.analysis = analysis
    capture_scans = CaptureScanHost()
    app.state.capture_scans = capture_scans
    diagnostics = DiagnosticHost()
    app.state.diagnostics = diagnostics
    planner = DiagnosticHost()  # a stabilised timeline's kept frame, for the preview

    router = APIRouter(prefix=API)

    @router.get("/health")
    def health() -> Health:
        return Health(
            version=__version__,
            build=build_id(),
            pid=os.getpid(),
            active_mounts=sum(mount.active for mount in mounts.listing().mounts),
        )

    @router.post("/engine/stop")
    def stop_engine(request: Request) -> dict[str, bool]:
        if request.client is None or not is_loopback(request.client.host):
            raise McError("only a loopback client may stop the engine", code="client-not-allowed")
        if offloader.running:
            raise McError("recordings are being copied", code="engine-busy")
        if any(mount.active for mount in mounts.listing().mounts):
            raise McError("the engine serves mounted media; unmount it before stopping", code="engine-busy")
        stopping.set()
        return {"stopping": True}

    @router.get("/events", response_class=StreamingResponse)
    async def events(request: Request) -> StreamingResponse:
        settings: InterfaceSettings = app.state.settings
        stream = _events(clips, request, stopping, analysis, mounts, previews, settings.preview_gpu, progress)
        headers = {"Cache-Control": "no-cache"}
        return StreamingResponse(stream, media_type="text/event-stream", headers=headers)

    @router.get("/doctor")
    def doctor() -> CheckList:
        checks = run_checks()
        problem: str | None = app.state.settings_problem
        if problem is not None:
            fix = "Save the settings in the Settings screen to replace the file"
            checks.append(Check(name="interface settings", status="warn", detail=problem, fix=fix))
        return CheckList(checks=checks)

    def licence_files() -> dict[str, Path]:
        folder = packaging_dir() / "LICENCES"
        return {path.name: path for path in sorted(folder.iterdir()) if path.is_file()}

    def packaged_text(path: Path) -> str:
        # Upstream notices are not all UTF-8; a stray byte must not hide the rest of the text.
        return path.read_bytes().decode("utf-8", errors="replace")

    @router.get("/about")
    def about() -> About:
        return About(
            version=__version__,
            checks=doctor().checks
            + (
                [Check(name="active preview backend", status="ok", detail=previews.backend_report)]
                if previews.backend_report is not None
                else []
            ),
            licences=list(licence_files()),
        )

    @router.get("/about/licences/{file}")
    def licence(file: str) -> Licence:
        path = licence_files().get(file)
        if path is None:
            raise McError(f"no licence file named {file}", code="input-missing")
        return Licence(file=file, text=packaged_text(path))

    app.include_router(router)
    app.include_router(
        mounts_router(
            API,
            mounts,
            find,
            lambda: app.state.settings.resolve_route,
            lambda path: _allowed(path, library()),
            progress,
        )
    )
    app.include_router(preview_router(API, lambda path: _allowed(path, library()), previews, find))
    app.include_router(settings_routes.router, prefix=API)
    app.include_router(cache_routes.router, prefix=API)
    app.include_router(reset_router(API, sessions))
    app.include_router(support_router(API))
    app.include_router(library_router(API, lambda path: _allowed(path, library()), clips, find))
    app.include_router(opened_router(API, sessions))
    app.include_router(sessions_router(API, sessions))
    app.include_router(offload_router(API, offloader))
    app.include_router(
        clips_router(API, find, analysis, mounts, sessions.clip_changed, sessions.remove_files)
    )
    app.include_router(vignette_router(API, find, mounts))
    app.include_router(annotations_router(API, find, sessions.clip_changed))
    app.include_router(reports_router(API, find))
    app.include_router(diagnostics_router(API, find, diagnostics, capture_scans, planner))
    app.include_router(colour_router(API))
    app.include_router(camera_profiles_router(API, find, diagnostics))
    app.include_router(cameras_router(API, find, diagnostics))
    add_interface(app)
    return app


def serve(app: FastAPI, config: ServerConfig) -> None:
    """Serve until Ctrl+C. Event streams end first, as uvicorn waits for open responses to finish."""

    class Server(uvicorn.Server):
        async def on_tick(self, counter: int) -> bool:
            if app.state.stopping.is_set():
                self.should_exit = True
            return await super().on_tick(counter)

        def handle_exit(self, sig: int, frame: FrameType | None) -> None:
            app.state.stopping.set()
            super().handle_exit(sig, frame)

    with suppress(KeyboardInterrupt):  # uvicorn raises the Ctrl+C again once it has stopped
        # no per-message compression: preview frames are JPEGs or 16-bit data, and compressing them only
        # adds latency (about half a second for a 1920-wide 16-bit frame)
        settings = uvicorn.Config(
            app, host=config.host, port=config.port, log_level="warning", ws_per_message_deflate=False
        )
        Server(settings).run()
