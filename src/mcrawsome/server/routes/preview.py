"""Preview routes: a preview session over a WebSocket and the clip's sound.

`WS {api}/clips/{id}/preview?path=CLIP[&width=1920]`: the client sends JSON requests `{"seq", "op", "index"?,
"recipe"?, "as"?}` (see preview/worker.py; `recipe` is a Recipe table, applied over the defaults). The
server sends a JSON header for each reply, `{"seq", "index", "kind", "ms", "bytes", ...}`, followed by a
binary message of `bytes` bytes when that is not 0: a JPEG for "draft" and "refined", 16-bit RGB for
"linear16". The first header says what was opened (`kind: "opened"`); errors
come as `kind: "error"` with the API's error object; `kind: "closed"` says another clip's preview took its
place. One preview runs at a time: opening another clip closes the one before.

A clip is resolved through its library id. The optional `path` query remains for scripts/bench_preview.py and
is checked against the library folders. `fast` selects reduced development; `playback` also suppresses
refinement and accepts `ahead` (1 to 3), with `buffer_end` on its last draft.

`source` selects full RAW image dimensions using spatial development and the display look, without
temporal/film effects or stabilisation. Source playback streams at most five frames ahead; `advance`
with `stream_seq` and `index` moves its audio clock without cancelling development. Stream replies
retain the originating sequence number and carry `stream: true`. Manual frame/recipe input ends it.
`native` additionally selects local BGR slot leases for desktop Source playback. These headers
have `bytes: 0` and a `display` descriptor; `release` returns that descriptor after drawing or
dropping it. Ordinary browsers retain JPEG playback. The opened header advertises `supports_source`;
ordinary video clips currently use Adaptive only.

`stream` with `playback` streams Adaptive drafts the same way (and `native` gives them as leases too),
instead of answering each `ahead` request; the opened header advertises `supports_stream`.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from mcrawsome.colour.white_balance import validate_clip_white_balance
from mcrawsome.develop.white_balance import pick_white_balance
from mcrawsome.errors import ErrorCode, McError
from mcrawsome.io.directlog import is_video_input
from mcrawsome.library.scan import ClipSummary
from mcrawsome.preview.audio import clip_audio
from mcrawsome.preview.display import DisplayLease
from mcrawsome.preview.session import PreviewSettings
from mcrawsome.preview.waveform import waveform
from mcrawsome.preview.worker import PreviewWorker, Reply, Spare
from mcrawsome.recipe.load import RecipeError, build_recipe
from mcrawsome.recipe.schema import Recipe
from mcrawsome.recipe.source import saved_video_recipe
from mcrawsome.results import ErrorInfo
from mcrawsome.server.routes.audio import raw_audio
from mcrawsome.server.routes.errors import ERROR_RESPONSES


class ClipWaveform(BaseModel):
    # The loudest sample in each of preview.waveform.POINTS equal parts of the clip, as a fraction of full
    # scale; none for a clip without sound.
    peaks: list[float]


class WhiteBalanceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    index: int = Field(ge=0, strict=True)
    rectangle: tuple[int, int, int, int]
    recipe: dict[str, Any] = Field(default_factory=dict)


class WhiteBalanceResult(BaseModel):
    temperature: float
    tint: float


class WhiteBalanceValidation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recipe: dict[str, Any]


_OPS = ("frame", "recipe", "refine", "linear16", "advance", "release")
SPARE_DELAY_S = 1.0  # after a window connects or a preview opens, before a spare process starts
_CLOSED_BY_ANOTHER = 4000  # the WebSocket close code when another clip's preview took this one's place


class PreviewHost:
    """The server's one preview worker: opening another clip's closes the one before.

    While a window is connected (`window_opened`), a spare preview process waits for the next clip, so
    opening one skips the process start-up. It is replaced SPARE_DELAY_S after each opening, once the new
    preview's first frames are under way, and ends when the last window goes."""

    def __init__(self, spare_delay_s: float = SPARE_DELAY_S) -> None:
        self._lock = asyncio.Lock()
        self._closing: set[asyncio.Task[None]] = set()
        self._current: PreviewWorker | None = None
        self.backend_report: str | None = None
        self._open: set[PreviewWorker] = set()  # the current one, and any still closing
        self._guard = threading.Lock()
        self._pending: set[threading.Event] = set()
        self._stopping = False
        self.spare_delay_s = spare_delay_s
        self._spare: Spare | None = None
        self._spare_timer: threading.Timer | None = None
        self._windows = 0

    def window_opened(self, gpu: bool) -> None:
        """A window connected: have a spare ready for its first preview."""
        with self._guard:
            self._windows += 1
        self.want_spare(gpu)

    def window_closed(self) -> None:
        """A window went; with none left, the spare ends (in the background: it may still be starting)."""
        with self._guard:
            self._windows = max(0, self._windows - 1)
            if self._windows:
                return
            spare, self._spare = self._spare, None
            timer, self._spare_timer = self._spare_timer, None
        if timer is not None:
            timer.cancel()
        if spare is not None:
            threading.Thread(target=spare.close, name="preview-spare-close", daemon=True).start()

    def want_spare(self, gpu: bool) -> None:
        """Start a spare shortly, unless one is ready or coming, or no window is connected."""
        with self._guard:
            if self._stopping or not self._windows or self._spare_timer is not None:
                return
            if self._spare is not None and self._spare.alive and self._spare.gpu == gpu:
                return
            timer = threading.Timer(self.spare_delay_s, self._make_spare, args=(gpu,))
            timer.daemon = True
            self._spare_timer = timer
        timer.start()

    def _make_spare(self, gpu: bool) -> None:
        with self._guard:
            self._spare_timer = None
            if self._stopping or not self._windows:
                return
            if self._spare is not None and self._spare.alive and self._spare.gpu == gpu:
                return
            replaced, self._spare = self._spare, None
        if replaced is not None:
            replaced.close()
        spare = Spare(gpu)
        with self._guard:
            keep = not self._stopping and self._windows > 0 and self._spare is None
            if keep:
                self._spare = spare
        if not keep:
            spare.close()

    def _take_spare(self) -> Spare | None:
        with self._guard:
            spare, self._spare = self._spare, None
        return spare

    async def open(self, clip: Path, recipe: Recipe, settings: PreviewSettings) -> PreviewWorker:
        async with self._lock:
            if self._current is not None:
                closing = asyncio.create_task(self._close(self._current))
                self._closing.add(closing)
                closing.add_done_callback(self._closing.discard)
                self._current = None
            self.backend_report = None
            cancelled = threading.Event()
            with self._guard:
                if self._stopping:
                    raise McError("preview service is stopping", code="cancelled")
                self._pending.add(cancelled)

            def start() -> PreviewWorker:
                try:
                    spare = self._take_spare()
                    worker = PreviewWorker(clip, recipe, settings, cancelled=cancelled, spare=spare)
                    with self._guard:
                        abandoned = self._stopping or cancelled.is_set()
                        if not abandoned:
                            self._open.add(worker)
                    if abandoned:
                        worker.close()
                        raise McError("preview opening cancelled", code="cancelled")
                    return worker
                finally:
                    with self._guard:
                        self._pending.discard(cancelled)

            opening = asyncio.create_task(asyncio.to_thread(start))
            try:
                worker = await asyncio.shield(opening)
            except asyncio.CancelledError:
                cancelled.set()
                # A completed constructor can win the cancellation race; it still needs an owner.
                with suppress(McError):
                    worker = await asyncio.shield(opening)
                    await asyncio.shield(self._close(worker))
                raise
            with self._guard:
                if self._stopping:
                    raise McError("preview service is stopping", code="cancelled")
                self._current = worker
            self.want_spare(settings.gpu)
            return worker

    async def release(self, worker: PreviewWorker) -> None:
        async with self._lock:
            if self._current is worker:
                self._current = None
                self.backend_report = None
        await self._close(worker)

    async def _close(self, worker: PreviewWorker) -> None:
        await asyncio.to_thread(worker.close)
        with self._guard:
            self._open.discard(worker)

    async def cache_usage(self, *, clear: bool = False) -> dict[str, int] | None:
        with self._guard:
            worker = self._current
        if worker is None:
            return None
        usage = await asyncio.to_thread(worker.cache_usage, clear=clear)
        with self._guard:
            if self._current is not worker:
                raise McError("Preview changed; refresh cache usage and try again", code="engine-busy")
        return usage

    def close(self) -> None:
        """At shutdown: every preview process ends, including one a departing client left closing, and the
        spare."""
        with self._guard:
            self._stopping = True
            for cancelled in self._pending:
                cancelled.set()
            opened = list(self._open)
            self._open.clear()
            self._current = None
            spare, self._spare = self._spare, None
            timer, self._spare_timer = self._spare_timer, None
        if timer is not None:
            timer.cancel()
        if spare is not None:
            spare.close()
        for worker in opened:
            worker.close()


def _error(seq: int, code: ErrorCode, message: str) -> Reply:
    return {"seq": seq, "kind": "error", "error": ErrorInfo(code=code, message=message).model_dump()}, b""


def _request(message: Any) -> dict[str, Any]:
    """A client message checked and with its Recipe built; McError("recipe-invalid") when it is wrong."""
    if not isinstance(message, dict):
        raise McError("a request must be a JSON object", code="recipe-invalid")
    seq, op = message.get("seq"), message.get("op")
    if not isinstance(seq, int) or isinstance(seq, bool) or seq < 1:
        raise McError("seq must be a whole number from 1", code="recipe-invalid")
    if op not in _OPS:
        raise McError(f"op must be one of {', '.join(_OPS)}", code="recipe-invalid")
    request: dict[str, Any] = {"seq": seq, "op": op}
    if op == "release":
        try:
            lease = DisplayLease.model_validate(message.get("display"))
        except ValueError as exc:
            raise McError(str(exc), code="recipe-invalid") from None
        return request | {"display": lease}
    if op == "advance":
        stream_seq, index = message.get("stream_seq"), message.get("index")
        if (
            not isinstance(stream_seq, int)
            or isinstance(stream_seq, bool)
            or stream_seq < 1
            or not isinstance(index, int)
            or isinstance(index, bool)
            or index < 0
        ):
            raise McError(
                "clock updates need a positive stream_seq and a non-negative whole index",
                code="recipe-invalid",
            )
        return request | {"stream_seq": stream_seq, "index": index}
    if "index" in message:
        if not isinstance(message["index"], int) or isinstance(message["index"], bool):
            raise McError("index must be a whole number", code="recipe-invalid")
        request["index"] = message["index"]
    elif op == "frame":
        raise McError("a frame request needs an index", code="recipe-invalid")
    if op == "recipe":
        layer = message.get("recipe")
        if not isinstance(layer, dict):
            raise McError("a recipe request needs a Recipe table in 'recipe'", code="recipe-invalid")
        try:
            request["recipe"] = build_recipe([layer])
        except RecipeError as exc:
            raise McError(str(exc), code="recipe-invalid") from None
    for flag in ("fast", "scrub", "source", "native", "stream"):
        if message.get(flag) is True:
            request[flag] = True
    if "ahead" in message:
        ahead = message["ahead"]
        if not isinstance(ahead, int) or isinstance(ahead, bool) or not 1 <= ahead <= 3:
            raise McError("playback look-ahead must be from 1 to 3", code="recipe-invalid")
        request["ahead"] = ahead
    if "warm_from" in message:
        warm_from = message["warm_from"]
        if not isinstance(warm_from, int) or isinstance(warm_from, bool) or warm_from < 0:
            raise McError("playback preparation needs a non-negative whole frame", code="recipe-invalid")
        request["warm_from"] = warm_from
    if message.get("playback") is True:
        request["playback"] = True
    if message.get("as") == "linear16":
        request["as"] = "linear16"
    return request


def preview_router(
    api: str, allowed: Callable[[Path], Path], host: PreviewHost, find: Callable[[str], ClipSummary]
) -> APIRouter:
    """The routes; `allowed` resolves a clip path that must lie in the library folders (else raises)."""
    router = APIRouter()

    def resolve(clip_id: str, path: Path | None) -> Path:
        return allowed(path) if path is not None else find(clip_id).path

    @router.post(f"{api}/clips/{{clip_id}}/white-balance/validate", responses=ERROR_RESPONSES)
    def validate_balance(
        clip_id: str, request: WhiteBalanceValidation, path: Path | None = None
    ) -> dict[str, bool]:
        source = resolve(clip_id, path)
        try:
            validate_clip_white_balance(source, build_recipe([request.recipe]))
        except RecipeError as exc:
            raise McError(str(exc), code="recipe-invalid") from None
        return {"valid": True}

    @router.post(f"{api}/clips/{{clip_id}}/white-balance", responses=ERROR_RESPONSES)
    def pick(clip_id: str, request: WhiteBalanceRequest, path: Path | None = None) -> WhiteBalanceResult:
        source = resolve(clip_id, path)
        try:
            recipe = build_recipe([request.recipe])
            kelvin, tint = pick_white_balance(source, recipe, request.index, request.rectangle)
            return WhiteBalanceResult(temperature=kelvin, tint=tint)
        except (RecipeError, ValueError) as exc:
            raise McError(str(exc), code="recipe-invalid") from None

    @router.get(f"{api}/clips/{{clip_id}}/audio", response_class=FileResponse)
    def audio(clip_id: str, request: Request, path: Path | None = None) -> Response:
        """The clip's sound as a 16-bit WAV, tempo-aligned to the labelled frame rate (as the DNG
        export's). The benchmark may still supply a library-checked `path`."""
        source = resolve(clip_id, path)
        if not is_video_input(source):
            return raw_audio(source, request.headers.get("range"))
        return FileResponse(clip_audio(source), media_type="audio/wav")

    @router.get(f"{api}/clips/{{clip_id}}/waveform", responses=ERROR_RESPONSES)
    def clip_waveform(clip_id: str, path: Path | None = None) -> ClipWaveform:
        """The clip's sound as peaks across its timeline, for the trim bar."""
        return ClipWaveform(peaks=waveform(resolve(clip_id, path)))

    @router.websocket(f"{api}/clips/{{clip_id}}/preview")
    async def preview(
        websocket: WebSocket,
        clip_id: str,
        path: Path | None = None,
        width: int = 1920,
        fast_width: int = 1280,
    ) -> None:
        """`width`: the preview's largest width in pixels (16 or more; default 1920)."""
        await websocket.accept()
        worker: PreviewWorker | None = None
        opening: asyncio.Task[PreviewWorker] | None = None
        receiving: asyncio.Task[Any] | None = None
        try:
            if not 16 <= fast_width <= 1920:
                raise McError("fast width must be from 16 to 1920", code="recipe-invalid")
            if width < 16:
                raise McError("width must be 16 pixels or more", code="recipe-invalid")
            settings = PreviewSettings(
                width=width,
                fast_width=fast_width,
                gpu=websocket.app.state.settings.preview_gpu,
                cache_mib=websocket.app.state.settings.preview_cache_mib,
                fast_open=True,
            )
            source = await asyncio.to_thread(resolve, clip_id, path)  # keeps the loop free for other clients
            seed = saved_video_recipe(source) if is_video_input(source) else build_recipe([])
            opening = asyncio.create_task(host.open(source, seed, settings))
            receiving = asyncio.create_task(websocket.receive_json())
            await asyncio.wait((opening, receiving), return_when=asyncio.FIRST_COMPLETED)
            if receiving.done():
                receiving.result()  # detect departure while the constructor is still opening
            worker = await asyncio.shield(opening)
            opened = {"kind": "opened", "frame_count": worker.frame_count, "width": worker.size[0]}
            opened |= {"height": worker.size[1], "fps": worker.fps, "rate": worker.rate, "bytes": 0}
            opened["supports_source"] = worker.supports_source
            opened["frame_width"], opened["frame_height"] = worker.frame_size
            opened["as_shot"] = worker.as_shot
            opened["supports_stream"] = True
            await websocket.send_json(opened)
            await _serve(websocket, worker, host, receiving)
        except McError as exc:
            with suppress(WebSocketDisconnect, RuntimeError):
                await websocket.send_json(_error(0, exc.code, exc.message)[0] | {"bytes": 0})
                await websocket.close(1008)
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            if receiving is not None:
                receiving.cancel()
                with suppress(asyncio.CancelledError, WebSocketDisconnect, RuntimeError):
                    await receiving
            if opening is not None:
                if not opening.done():
                    opening.cancel()
                with suppress(asyncio.CancelledError, McError):
                    completed = await asyncio.shield(opening)
                    worker = completed
            if worker is not None:
                await asyncio.shield(host.release(worker))

    return router


async def _serve(
    websocket: WebSocket,
    worker: PreviewWorker,
    host: PreviewHost,
    first: asyncio.Task[Any] | None = None,
) -> None:
    """Forward requests to the worker and its replies back, until either side goes."""
    loop = asyncio.get_running_loop()
    replies: asyncio.Queue[Reply | None] = asyncio.Queue(maxsize=3)
    stop = threading.Event()

    def enqueue(reply: Reply | None) -> bool:
        pending = asyncio.run_coroutine_threadsafe(replies.put(reply), loop)
        while not stop.is_set():
            try:
                pending.result(timeout=0.2)
                return True
            except TimeoutError:
                continue
        pending.cancel()
        return False

    def read() -> None:  # the worker's pipe is read on a thread of its own
        while not stop.is_set():
            try:
                reply = worker.receive(0.2)
            except (EOFError, OSError):
                enqueue(None)
                return
            if reply is not None and not enqueue(reply):
                return

    async def forward() -> None:
        try:
            while (reply := await replies.get()) is not None:
                header, payload = reply  # "sent": when it left, in ms since 1970 (a page's transfer time)
                if worker is host._current and "backend" in header:
                    host.backend_report = f"{header['backend']} ({header['backend_reason']})"
                await websocket.send_json(header | {"bytes": len(payload), "sent": time.time() * 1000})
                if payload:
                    await websocket.send_bytes(payload)
            if not stop.is_set():  # the worker ended without this side asking: another clip took its place
                await websocket.send_json({"kind": "closed", "bytes": 0})
                await websocket.close(_CLOSED_BY_ANOTHER)
        except (WebSocketDisconnect, RuntimeError):  # the client went first
            return

    reader = threading.Thread(target=read, name="preview-replies", daemon=True)
    reader.start()
    forwarding = asyncio.create_task(forward())
    try:
        while True:
            if first is not None:
                message = await first
                first = None
            else:
                message = await websocket.receive_json()
            try:
                request = _request(message)
            except McError as exc:
                seq = message.get("seq") if isinstance(message, dict) else None
                reply, payload = _error(seq if isinstance(seq, int) else 0, exc.code, exc.message)
                if isinstance(message, dict) and message.get("op") == "linear16":
                    reply["purpose"] = "linear16"
                await replies.put((reply, payload))
                continue
            try:
                await asyncio.to_thread(worker.send, request)
            except (OSError, ValueError):
                break  # the worker has gone: forward() says so
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        stop.set()
        await asyncio.to_thread(reader.join)
        forwarding.cancel()
        with suppress(asyncio.CancelledError):
            await forwarding
