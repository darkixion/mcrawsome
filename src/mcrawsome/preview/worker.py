"""A preview session in its own spawned process, serving the newest request first.

Requests are dicts with a sequence number: `{"seq", "op", "index"?, "recipe"?, "as"?}`, where `op` is "frame"
(show frame `index`), "recipe" (show the kept frame with a new Recipe), "refine" (the refined frame now),
or "linear16" (the kept frame before the display rendering, as 16-bit codes). Each reply is a JSON-ready
header and a payload. A frame or recipe request is answered with a draft JPEG at once and, when no newer
request has come for SETTLE_S, with the refined frame. Requests that arrive while one is being made are
read together and only the newest is answered; a reply made for a request that has since been overtaken is
never sent ("newest wins").

`fast` uses reduced native development and skips film effects. `playback` also suppresses refinement;
`ahead` asks for one to three consecutive drafts, the last labelled `buffer_end`. Each is sent at once.
Manual requests still supersede playback.

`source` develops every RAW photosite, with the fast path's spatial processing and display look.
It skips temporal/film effects and stabilisation. With `playback`, a persistent producer overlaps
development and display encoding, with bounded queues and display slots. `advance` updates its monotonic audio
clock (`stream_seq` identifies the originating request); it does not supersede that request. Source
replies keep its sequence number and carry `stream: true`. A manual frame or recipe ends the stream.

`stream` (with `playback`, without `source`) plays Adaptive drafts through the same producer, at the
playback width and adaptively ahead of the clock; the page asks for it when the opened header offers it.

`native` playback publishes bounded local BGR leases instead of JPEG payloads.
`release` acknowledges a slot token after drawing or dropping it; it never supersedes a frame.
Normal exit and forced process cleanup unlink the local files.

An automatic exposure is measured in the background as an export measures it (several seconds for a long
clip); until then frames show 0 stops and say so in the header (`exposure.measuring`), and the newest frame
is sent again once the value is known.
"""

from __future__ import annotations

import contextlib
import ctypes as ct
import importlib
import multiprocessing as mp
import os
import signal
import threading
import time
import traceback
from collections.abc import Callable
from concurrent.futures import CancelledError, Future, ThreadPoolExecutor
from multiprocessing import resource_tracker, shared_memory
from multiprocessing.connection import Connection
from multiprocessing.process import BaseProcess
from pathlib import Path
from types import TracebackType
from typing import Any, Self

from mcrawsome.colour.white_balance import as_shot_values
from mcrawsome.errors import ErrorCode, McError, error_code
from mcrawsome.io.clip import Clip
from mcrawsome.io.directlog import is_video_input
from mcrawsome.pipeline.priority import background_priority
from mcrawsome.pipeline.shared import block_prefix
from mcrawsome.preview.backend import gpu_enabled
from mcrawsome.preview.display import DisplayFrames, DisplayLease
from mcrawsome.preview.opencl import prepare_runtime
from mcrawsome.preview.prefetch import Draft, PlaybackStream
from mcrawsome.preview.preparation import warm_frames
from mcrawsome.preview.session import PreviewSession, PreviewSettings, Refined
from mcrawsome.recipe.schema import Recipe

SETTLE_S = 0.3  # a draft's refined frame follows once no newer request has come for this long
OPEN_S = 120.0  # opening reads the clip's facts (including a long video's probe)
CLOSE_S = 5.0  # how long the process may take to end on its own before it is stopped

Header = dict[str, Any]
Reply = tuple[Header, bytes]
_FRAME_OPS = ("frame", "recipe", "refine", "linear16")


def _remove_blocks(pid: int) -> None:
    """Reclaim a stopped preview's frame rings, display slots and pool semaphores.

    The process group has ended before this call. Other previews and the server use different names.
    Remove tracked resources now rather than leaving them until the server's tracker exits.
    """
    for path in Path("/dev/shm").glob(f"mcrawsome-preview-{pid}-*"):
        path.unlink(missing_ok=True)
    for path in Path("/dev/shm").glob(f"{block_prefix(pid)}*"):
        try:
            block = shared_memory.SharedMemory(name=path.name)
        except (FileNotFoundError, OSError):
            continue
        block.close()
        with contextlib.suppress(FileNotFoundError):
            block.unlink()
    from _multiprocessing import sem_unlink  # type: ignore[import-not-found]

    for path in Path("/dev/shm").glob(f"sem.mcrawsome_p{pid}-*"):
        name = f"/{path.name.removeprefix('sem.')}"
        # As with attaching SharedMemory above, registering first also covers a process killed
        # between creating the resource and registering it. The shared tracker stores a set.
        resource_tracker.register(name, "semaphore")
        try:
            with contextlib.suppress(FileNotFoundError):
                sem_unlink(name)
        finally:
            resource_tracker.unregister(name, "semaphore")


def _error(seq: int, code: ErrorCode, message: str, purpose: str = "frame") -> Reply:
    return {"seq": seq, "kind": "error", "purpose": purpose, "error": {"code": code, "message": message}}, b""


class Spare:
    """A preview process started before it is wanted: set up, with the imports a session loads on first use
    done, and waiting for the clip a PreviewWorker gives it. Opening a preview with one skips about half a
    second of process start-up."""

    def __init__(self, gpu: bool) -> None:
        context = mp.get_context("spawn")
        self.gpu = gpu
        self._lock = threading.Lock()
        self._taken = False
        self._conn, child = context.Pipe()
        self._cache_conn, cache_child = context.Pipe()
        self._input_seq = context.RawValue(ct.c_longlong, 0)
        # not a daemon: the session's refined frames and measurements use a pool of their own
        self._process = context.Process(
            target=_wait, args=(child, gpu, self._input_seq, cache_child), name="mcrawsome-preview"
        )
        self._process.start()
        child.close()
        cache_child.close()

    @property
    def alive(self) -> bool:
        return not self._taken and self._process.is_alive()

    def take(
        self, clip_path: Path, recipe: Recipe, settings: PreviewSettings
    ) -> tuple[BaseProcess, Connection, Connection, Any] | None:
        """The process, given its clip, with its connections and newest-request counter; None (and the
        process ended) when it can no longer take one."""
        with self._lock:
            if self._taken:
                return None
            self._taken = True
        if self._process.is_alive():
            try:
                self._conn.send(("open", clip_path, recipe, settings))
                return self._process, self._conn, self._cache_conn, self._input_seq
            except (OSError, ValueError):
                pass
        self._end()
        return None

    def close(self) -> None:
        """End the process unused. Harmless twice, and after `take`."""
        with self._lock:
            if self._taken:
                return
            self._taken = True
        self._end()

    def _end(self) -> None:
        self._conn.close()  # a waiting process reads the end of its pipe and returns
        self._cache_conn.close()
        self._process.join(CLOSE_S)
        for stop in (signal.SIGTERM, signal.SIGKILL):
            if self._process.is_alive():
                _signal_group(self._process, stop)
                self._process.join(5)
        if self._process.pid is not None:
            _remove_blocks(self._process.pid)


class PreviewWorker:
    """`PreviewSession(clip_path, recipe, settings)` in a spawned process, or in a `spare` started before.
    `send` a request, `receive` replies; a reply to a request overtaken by a newer one is dropped here too."""

    def __init__(
        self,
        clip_path: Path,
        recipe: Recipe,
        settings: PreviewSettings | None = None,
        *,
        cancelled: threading.Event | None = None,
        spare: Spare | None = None,
    ) -> None:
        if cancelled is not None and cancelled.is_set():
            if spare is not None:
                spare.close()
            raise McError("preview opening cancelled", code="cancelled")
        self._closing, self._closed = threading.Lock(), False
        self._cache_lock = threading.Lock()
        self._cache_seq = 0
        settings = settings or PreviewSettings()
        taken = spare.take(clip_path, recipe, settings) if spare is not None else None
        if taken is not None:
            self._process, self._conn, self._cache_conn, self._input_seq = taken
        else:
            context = mp.get_context("spawn")
            self._conn, child = context.Pipe()
            self._cache_conn, cache_child = context.Pipe()
            self._input_seq = context.RawValue(ct.c_longlong, 0)
            # not a daemon: the session's refined frames and measurements use a pool of their own
            self._process = context.Process(
                target=_serve,
                args=(child, clip_path, recipe, settings, self._input_seq, cache_child),
                name="mcrawsome-preview",
            )
            self._process.start()
            child.close()
            cache_child.close()
        self._sending = threading.Lock()
        self._newest = 0
        self._opened = False
        opened: Reply | None = None
        deadline = time.monotonic() + OPEN_S
        try:
            while time.monotonic() < deadline and not (cancelled is not None and cancelled.is_set()):
                if self._conn.poll(min(0.05, max(0, deadline - time.monotonic()))):
                    opened = self._conn.recv()
                    break
        except EOFError:
            opened = None
        if cancelled is not None and cancelled.is_set():
            self.close()
            raise McError("preview opening cancelled", code="cancelled")
        if opened is None or opened[0]["kind"] != "opened":
            self.close()
            if opened is None:
                raise McError(f"{clip_path.name}: the preview process did not start", code="internal-error")
            error = opened[0]["error"]
            raise McError(error["message"], code=error["code"])
        header = opened[0]
        self.clip_path = clip_path
        self.frame_count: int = header["frame_count"]
        self.size: tuple[int, int] = (header["width"], header["height"])
        self.frame_size: tuple[int, int] = (header["frame_width"], header["frame_height"])
        self.fps: float = header["fps"]  # measured
        self.rate: float = header["rate"]  # labelled: the sound is tempo-aligned to it
        self.supports_source: bool = header.get("supports_source", False)
        self.as_shot: dict[str, float | None] = header.get("as_shot", {})
        self._opened = True

    @property
    def alive(self) -> bool:
        return self._process.is_alive()

    def send(self, request: dict[str, Any]) -> None:
        with self._sending:
            if request.get("op") in _FRAME_OPS:
                self._newest = max(self._newest, int(request["seq"]))
                self._input_seq.value = self._newest
            self._conn.send(request)

    def receive(self, timeout: float) -> Reply | None:
        """The next reply within `timeout` seconds, or None. Raises EOFError once the process has ended."""
        deadline = time.monotonic() + timeout
        while self._conn.poll(max(0.0, deadline - time.monotonic())):
            header, payload = self._conn.recv()
            if (header["kind"] == "error" and header.get("purpose", "frame") != "frame") or header.get(
                "seq", 0
            ) >= self._newest:
                if "frame_count" in header:
                    self.frame_count = header["frame_count"]
                    self.rate = header["rate"]
                return header, payload
            if "display" in header:
                self.send(
                    {
                        "seq": self._newest,
                        "op": "release",
                        "display": DisplayLease.model_validate(header["display"]),
                    }
                )
        return None

    def cache_usage(self, *, clear: bool = False) -> dict[str, int]:
        """Worker-owned maintenance, independent of the frame receiver and newest-frame sequence."""
        with self._cache_lock:
            self._cache_seq += 1
            sequence = self._cache_seq
            try:
                self.send({"op": "cache", "cache_seq": sequence, "clear": clear})
                deadline = time.monotonic() + CLOSE_S
                while time.monotonic() < deadline:
                    if self._cache_conn.poll(min(0.05, max(0, deadline - time.monotonic()))):
                        reply_sequence, usage = self._cache_conn.recv()
                        if reply_sequence == sequence:
                            return dict(usage)
                    if not self.alive:
                        break
            except (EOFError, OSError, ValueError):
                pass
            raise McError("Preview cache is busy or closed; refresh and try again", code="engine-busy")

    def close(self) -> None:
        """End the process (stopping any work under way), and hand back its memory. Harmless twice, and from
        two threads at once."""
        with self._closing:
            if not self._closed:
                self._close()
                self._closed = True

    def _close(self) -> None:
        if self._process.is_alive():
            try:
                with self._sending:
                    self._conn.send({"op": "close"})
            except (OSError, ValueError):
                pass
            # An unfinished constructor cannot service a close message yet. Do not hold the next
            # clip up for the full grace period; it has no running frame work to settle.
            self._process.join(CLOSE_S if self._opened else 0.1)
            for stop in (signal.SIGTERM, signal.SIGKILL):
                if self._process.is_alive():
                    self._signal_group(stop)
                    self._process.join(5)
        self._process.join()
        self._signal_group(signal.SIGKILL)  # a refined frame's or a measurement's pool left behind
        if self._process.pid is not None:
            _remove_blocks(self._process.pid)
        self._conn.close()
        with self._cache_lock:
            self._cache_conn.close()

    def _signal_group(self, stop: signal.Signals) -> None:
        _signal_group(self._process, stop)

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        self.close()


def _signal_group(process: BaseProcess, stop: signal.Signals) -> None:
    """A preview process and everything it started (it leads a process group of its own)."""
    if process.pid is not None:
        try:
            os.killpg(process.pid, stop)
        except (ProcessLookupError, PermissionError):
            # Cancellation can arrive before the spawned child has called setpgrp().
            if process.is_alive():
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.kill(process.pid, stop)


def _prepare(gpu: bool) -> None:
    """A preview process's set-up, the same for every clip."""
    signal.signal(signal.SIGINT, signal.SIG_IGN)  # Ctrl+C is the server's: it closes this process
    os.setpgrp()  # so closing can end the pools a refined frame or a measurement started
    # Pool semaphores inherit this name, so forced close can identify only this preview's resources.
    mp.current_process()._config["semprefix"] = f"/mcrawsome_p{os.getpid()}"  # type: ignore[attr-defined]
    if gpu:
        _discover_drivers()


def _discover_drivers() -> None:
    if gpu_enabled():
        # Driver discovery must finish before playback readers can open concurrently.
        # Backend selection still reports unavailable devices and falls back to CPU.
        with contextlib.suppress(OSError, RuntimeError):
            prepare_runtime()


def _preload() -> None:
    """Import what opening a session loads on first use: OpenColorIO for the display rendering."""
    with contextlib.suppress(ImportError):
        importlib.import_module("PyOpenColorIO")


def _wait(conn: Connection, gpu: bool, input_seq: ct.c_longlong, cache_conn: Connection) -> None:
    """A spare preview process: its set-up and imports, then the clip it is given (see Spare.take)."""
    _prepare(gpu)
    _preload()
    try:
        message = conn.recv()
    except (EOFError, OSError):
        return  # closed unused
    if not (isinstance(message, tuple) and len(message) == 4 and message[0] == "open"):
        return
    _, clip_path, recipe, settings = message
    if settings.gpu and not gpu:
        _discover_drivers()
    _session(conn, clip_path, recipe, settings, input_seq, cache_conn)


def _serve(
    conn: Connection,
    clip_path: Path,
    recipe: Recipe,
    settings: PreviewSettings,
    input_seq: ct.c_longlong,
    cache_conn: Connection,
) -> None:
    """The preview process: open the session, then answer requests until told to close or the server
    goes."""
    _prepare(settings.gpu)
    _session(conn, clip_path, recipe, settings, input_seq, cache_conn)


def _session(
    conn: Connection,
    clip_path: Path,
    recipe: Recipe,
    settings: PreviewSettings,
    input_seq: ct.c_longlong,
    cache_conn: Connection,
) -> None:
    try:
        session = PreviewSession(clip_path, recipe, settings)
    except Exception as exc:
        conn.send(_error(0, _code(exc), _message(exc)))
        return
    with session:
        (width, height), (frame_width, frame_height) = session.size, session.full_size
        opened = {"kind": "opened", "frame_count": session.frame_count, "width": width, "height": height}
        opened |= {"frame_width": frame_width, "frame_height": frame_height, "fps": session.fps}
        opened["rate"] = session.rate
        opened["supports_source"] = session.supports_source
        if session.supports_source:
            with Clip(clip_path) as clip:
                opened["as_shot"] = as_shot_values(clip)
        conn.send((opened, b""))
        loop = _Loop(conn, session, newest=lambda: input_seq.value, cache_conn=cache_conn)
        try:
            loop.run()
        finally:
            # Background work owns render pools and reads the session. Join it before closing the
            # clip, so spawned pools can release their semaphores through normal process cleanup.
            loop.close()
    conn.close()
    cache_conn.close()


def _code(exc: BaseException) -> ErrorCode:
    if isinstance(exc, McError):
        return exc.code
    if isinstance(exc, IndexError | KeyError | ValueError):
        return "recipe-invalid"
    return error_code(exc, "render-failed")


def _message(exc: BaseException) -> str:
    if isinstance(exc, KeyError) and exc.args:
        return str(exc.args[0])
    return str(exc) or type(exc).__name__


class _Loop:
    """The preview process's requests and its two background tasks (a refined frame, a measurement)."""

    def __init__(
        self,
        conn: Connection,
        session: PreviewSession,
        *,
        newest: Callable[[], int] | None = None,
        cache_conn: Connection | None = None,
    ) -> None:
        self.conn, self.session = conn, session
        self.cache_conn = cache_conn
        self._input_seq = newest
        if session.settings.gpu and gpu_enabled():
            session.pipeline.prepare()
        else:
            session.backend.prepare()
        self.background = ThreadPoolExecutor(2, thread_name_prefix="preview", initializer=background_priority)
        self.fast_mode = False
        self.playing = False
        self.source_mode = False
        self.source_stream: PlaybackStream | None = None
        self.display_frames: DisplayFrames | None = None
        self._source_seq = 0
        self.scrubbing = False
        self.warming: list[tuple[int, int]] = []
        self.warm_from = 0
        self.warm_due = 0.0
        self._preferred_width = min(session.settings.fast_width, session.settings.width)
        self.playback_width = self._preferred_width
        self._slow_drafts = 0
        self._small_drafts = 0
        self.newest = 0  # the newest frame request's seq
        self.index = 0  # the frame shown
        self._draft_revision = 0
        self._refining_revision = 0
        self.shown: tuple[int, int] | None = None  # (seq, index) of the newest draft sent
        self.refine_due: tuple[float, int, int] | None = None  # (when, seq, index)
        self.refining: tuple[Future[Refined], int, int, float] | None = None  # (task, seq, index, start)
        self.measure_due: float | None = None
        self._analysis_cancel = threading.Event()
        self.measuring: tuple[Future[float], str] | None = None
        self.auto_error: str | None = None
        self.backlog: list[dict[str, Any]] = []  # requests read while deciding whether a reply is stale

    def run(self) -> None:
        try:
            while True:
                if self.backlog or self.conn.poll(self._timeout()):
                    batch = self._drain()
                    if batch is None:
                        return
                    self._answer(batch, time.perf_counter())
                self._pump_source()
                self._background()
                if not self.backlog and not self.conn.poll(0) and time.monotonic() >= self.warm_due:
                    self._warm()
        except (EOFError, OSError):  # the server went
            return
        finally:
            self.background.shutdown(wait=False, cancel_futures=True)

    def close(self) -> None:
        """Wait for the background work under way (the tests' way to end a loop)."""
        self._analysis_cancel.set()
        self.session.close_pipeline()
        if self.display_frames is not None:
            self.display_frames.close()
        self.background.shutdown(wait=True, cancel_futures=True)

    @property
    def busy(self) -> bool:
        return self.refining is not None or self.measuring is not None

    def _timeout(self) -> float | None:
        waits = []
        if self.source_stream is not None:
            waits.append(0.005)
        if self.warming:
            waits.append(max(0.005, self.warm_due - time.monotonic()))
        if self.measure_due is not None and self.measuring is None:
            waits.append(max(0.0, self.measure_due - time.monotonic()))
        if self.refine_due is not None:
            waits.append(max(0.0, self.refine_due[0] - time.monotonic()))
        if self.refining is not None or self.measuring is not None:
            waits.append(0.02)  # look at the background tasks this often
        return min(waits) if waits else None

    def _drain(self) -> list[dict[str, Any]] | None:
        """Every request waiting (those already read first), oldest first; None when told to close."""
        batch, self.backlog = self.backlog, []
        if not batch:
            batch.append(self.conn.recv())
        batch += self._waiting()
        self.backlog = []
        return None if any(request.get("op") == "close" for request in batch) else batch

    def _waiting(self) -> list[dict[str, Any]]:
        """The requests not answered yet, reading those in the pipe into the backlog."""
        while self.conn.poll(0):
            self.backlog.append(self.conn.recv())
        return self.backlog

    def _send(self, reply: Reply, seq: int | None = None) -> bool:
        """Send, unless the reply answers request `seq` and a newer frame request has come (then it is
        dropped); True when sent. Other requests waiting (a table) do not make a reply stale."""
        if seq is not None:
            newer = (
                request.get("op") in _FRAME_OPS and int(request.get("seq", 0)) > seq
                for request in self._waiting()
            )
            if seq < self.newest or any(newer):
                return False
        self.conn.send(reply)
        return True

    def _answer(self, batch: list[dict[str, Any]], received: float) -> None:
        for request in batch:
            if request.get("op") == "cache" and self.cache_conn is not None:
                if request.get("clear"):
                    self.warming.clear()
                    self.session.clear_cache()
                self.cache_conn.send((request["cache_seq"], self.session.cache_usage()))
            if request.get("op") == "release" and self.display_frames is not None:
                self.display_frames.release(request["display"])
            if (
                request.get("op") == "advance"
                and self.source_stream is not None
                and request.get("stream_seq") == self._source_seq
            ):
                self.source_stream.advance(int(request["index"]))
        batch = [request for request in batch if request.get("op") not in ("advance", "release", "cache")]
        if not batch:
            return
        frames = [request for request in batch if request.get("op") in _FRAME_OPS]
        if self.source_stream is None or frames:
            self.source_stream = None
            self.session.cancel_pending()
            self.session.pipeline.stop_stream()
            self.warming.clear()
        latest: dict[str, Any] | None = None
        recipe: tuple[int, Recipe] | None = None
        recipe_error: Exception | None = None

        def apply_recipe() -> None:
            nonlocal recipe, recipe_error
            if recipe is None:
                return
            old_rate = self.session.rate
            try:
                if self.fast_mode:
                    self.session.set_recipe(recipe[1], fast=True)
                else:
                    self.session.set_recipe(recipe[1])
            except Exception as exc:
                recipe_error = exc
            else:
                recipe_error = None
                self.auto_error = None
                if self.session.rate != old_rate:
                    self.index = round(self.index / old_rate * self.session.rate)
                self.index = max(0, min(self.index, self.session.frame_count - 1))
            recipe = None

        if frames:
            self._analysis_cancel.set()
            frame_seq = int(frames[-1]["seq"])
            newest = self._input_seq
            self.session.backend.cancelled = (lambda: newest() > frame_seq) if newest is not None else None
            self.playing = frames[-1].get("playback") is True
            self.scrubbing = frames[-1].get("scrub") is True
            self.source_mode = frames[-1].get("source") is True and not self.scrubbing
            self.fast_mode = self.playing or frames[-1].get("fast") is True
        for request in batch:
            op, seq = request.get("op"), int(request.get("seq", 0))
            if op in _FRAME_OPS:
                self.newest, latest = max(self.newest, seq), request
                if op == "recipe":
                    recipe = (seq, request["recipe"])
                if "index" in request:
                    self.index = int(request["index"])
            else:
                self._send(_error(seq, "recipe-invalid", f"unknown request {op!r}"))
        apply_recipe()
        if recipe_error is not None and latest is not None:
            seq = int(latest["seq"])
            self._send(_error(seq, _code(recipe_error), _message(recipe_error)), seq)
            return
        if latest is None:
            return
        seq = int(latest["seq"])
        self.warm_from = int(latest.get("warm_from", self.index))
        self.refine_due = None
        self.warming = []
        try:
            if self.playing and (self.source_mode or latest.get("stream") is True):
                if self.source_mode and not self.session.supports_source:
                    raise ValueError("source-resolution preview is currently available for RAW clips")
                self._source_seq = seq
                display = None
                if latest.get("native") is True:
                    if self.display_frames is None:
                        width, height = self.session.full_size
                        self.display_frames = DisplayFrames(width * height)
                    display = self.display_frames
                self.source_stream = self.session.pipeline.stream(
                    self.index,
                    display=display,
                    width=None if self.source_mode else self.playback_width,
                )
                self._draft_revision += 1
            elif self.source_mode:
                self._draft(seq, received)
            elif latest["op"] == "refine":
                self.refine_due = (time.monotonic(), seq, self.index)
            elif latest["op"] == "linear16" or latest.get("as") == "linear16":
                self._send(self._linear16(seq, received), seq)
            else:
                ahead = min(3, max(1, int(latest.get("ahead", 1)))) if self.playing else 1
                first = self.index
                last = min(self.session.frame_count, first + ahead)
                pending = (
                    {}
                    if self.session.pipeline.serial_gpu
                    else {
                        index: self.session.pipeline.submit(index, self.playback_width)
                        for index in range(first + 1, last)
                    }
                )
                for index in range(first, last):
                    self.index = index
                    if index > first and any(r.get("op") in _FRAME_OPS for r in self._waiting()):
                        break
                    task = pending.get(index)
                    self._draft(
                        seq,
                        time.perf_counter() if index > first else received,
                        playback=self.playing,
                        buffer_end=index == last - 1,
                        prepared=task.result() if task is not None else None,
                    )
                if first >= self.session.frame_count or first < 0:
                    self.session.keep(first)  # the usual out-of-range error
        except CancelledError:
            return  # the newest request is already in the pipe
        except Exception as exc:
            purpose = "linear16" if latest["op"] == "linear16" else "frame"
            self._send(_error(seq, _code(exc), _message(exc), purpose), seq)
        self.measure_due = None if self.playing or self.scrubbing else time.monotonic() + SETTLE_S

    def _pump_source(self) -> None:
        if self.source_stream is None:
            return
        try:
            for _ in range(2):
                frame = self.source_stream.receive()
                if frame is None:
                    return
                self.index, draft = frame
                received = time.perf_counter() - draft.timings.get("total_ms", 0.0) / 1000
                self._draft(self._source_seq, received, playback=True, stream=True, prepared=draft)
                if self.source_stream is not None and self.source_stream.width is not None:
                    self.source_stream.width = self.playback_width  # smaller drafts while they are slow
        except CancelledError:
            return
        except Exception as exc:
            self.source_stream = None
            self.session.pipeline.stop_stream()
            self._send(_error(self._source_seq, _code(exc), _message(exc)), self._source_seq)

    def _draft(
        self,
        seq: int,
        received: float,
        *,
        playback: bool = False,
        buffer_end: bool = True,
        prepared: Draft | None = None,
        stream: bool = False,
    ) -> None:
        if self.source_mode:
            prepared = prepared or self.session.pipeline.render_source(self.index)
            frame = prepared.frame
        elif playback or self.fast_mode:
            width = min(640, self._preferred_width) if self.scrubbing else self.playback_width
            if prepared is None and self.session.pipeline.serial_gpu:
                prepared = self.session.pipeline.render(self.index, width, drag=self.scrubbing)
            frame = (
                prepared.frame
                if prepared is not None
                else self.session.fast(self.index, width=width, drag=self.scrubbing)
            )
        else:
            frame = Refined(self.session.frame(self.index), size=self.session.size)
        width, height = frame.size or self.session.size
        header = self._header(seq, "draft", received) | {"width": width, "height": height}
        fast = playback or self.fast_mode or self.source_mode
        if self.source_mode:
            header["source"] = True
        header["backend"] = (
            prepared.backend if prepared is not None else (self.session.backend.name if fast else "CPU")
        )
        if fast:
            header["timings"] = prepared.timings if prepared is not None else self.session.fast_timings
        header["backend_reason"] = (
            prepared.reason
            if prepared is not None
            else (self.session.backend.reason if fast else "full preview")
        )
        if frame.display is not None:
            header["display"] = frame.display.model_dump()
        if playback:
            header["playback"] = True
            if stream:
                header["stream"] = True
            else:
                header["buffer_end"] = buffer_end
        sent = self._send((header, frame.jpeg), seq)
        if not sent:
            frame.discard()
        if sent:
            self._draft_revision += 1
            self.shown = (seq, self.index)
            self.refine_due = (
                None
                if playback or self.scrubbing or self.source_mode
                else (time.monotonic() + SETTLE_S, seq, self.index)
            )
            if (
                not playback
                and not self.source_mode
                and not self.scrubbing
                and self.session.settings.cache_mib
            ):
                self.warm_due = time.monotonic() + 0.06
                scale = max(1.0, self._preferred_width / self.playback_width)
                self.warming = warm_frames(
                    self.index,
                    self.warm_from,
                    self.session.frame_count,
                    self.session.rate,
                    self._preferred_width,
                    (round(width * scale), round(height * scale)),
                    self.session.settings.cache_mib,
                    raw=not is_video_input(self.session.clip_path),
                )
        # Ignore a single cold frame. Probe the preferred size again after a second.
        if playback and not self.source_mode:
            if stream and prepared is not None:
                # Streamed stages overlap: a draft is slow when its busier stage takes longer than a frame.
                times = prepared.timings
                busy = max(
                    times.get("decode_ms", 0.0) + times.get("develop_ms", 0.0),
                    times.get("finish_ms", 0.0)
                    + times.get("jpeg_ms", 0.0)
                    + times.get("display_copy_ms", 0.0),
                )
                slow = busy / 1000 > 1 / self.session.rate * 1.2
            else:
                slow = time.perf_counter() - received > 1 / self.session.rate * 1.2
            self._slow_drafts = min(3, self._slow_drafts + 1) if slow else max(0, self._slow_drafts - 1)
            if (
                self.playback_width == self._preferred_width
                and self.playback_width > 640
                and self._slow_drafts == 3
            ):
                self.playback_width, self._small_drafts = 640, 0
            elif self.playback_width == 640:
                self._small_drafts += 1
                if self._small_drafts >= round(self.session.rate):
                    self.playback_width, self._slow_drafts = self._preferred_width, 0
        else:
            self.playback_width, self._slow_drafts = self._preferred_width, 0

    def _warm(self) -> None:
        """Dispatch at most two speculative frames, without waiting for either lane.

        Readers, OpenCL queues and buffers belong to the lanes. Any input signals cancellation
        before its recipe is applied; cancelled work can only write to its old cache generation.
        """
        while self.warming and not self.playing:
            index, width = self.warming[0]
            if self.session.pipeline.submit(index, width) is None:
                return
            self.warming.pop(0)

    def _linear16(self, seq: int, received: float) -> Reply:
        self.session.keep(self.index)
        data, (width, height) = self.session.linear16()
        header = self._header(seq, "linear16", received) | {"width": width, "height": height}
        return header, data

    def _header(self, seq: int, kind: str, since: float) -> Header:
        session = self.session
        auto = session.recipe.develop.exposure == "auto"
        stops = session.auto_stops() if auto else float(session.recipe.develop.exposure)
        exposure: dict[str, Any] = {"auto": auto, "stops": stops, "measuring": session.auto_pending}
        if auto and self.auto_error is not None:
            exposure |= {"measuring": False, "error": self.auto_error}
        return {
            "seq": seq,
            "index": self.index,
            "kind": kind,
            "ms": round((time.perf_counter() - since) * 1000, 1),
            "label": session.label,
            "exposure": exposure,
            "frame_width": session.full_size[0],
            "frame_height": session.full_size[1],
            "frame_count": session.frame_count,
            "rate": session.rate,
        }

    def _measure(self) -> None:
        """Measure an unknown automatic exposure once interactive inputs have settled."""
        if self.playing or self.scrubbing or self.measuring is not None or self.auto_error is not None:
            return
        if self.measure_due is None or time.monotonic() < self.measure_due:
            return
        self.measure_due = None
        self._analysis_cancel = threading.Event()
        measurer = self.session.auto_measurer(cancelled=self._analysis_cancel.is_set)
        if measurer is not None:
            key, measure = measurer
            self.measuring = (self.background.submit(measure), key)

    def _background(self) -> None:
        if self.measuring is not None and self.measuring[0].done():
            measured, key = self.measuring
            self.measuring = None
            waiting = self.session.auto_pending
            try:
                stops = measured.result()
                if not self._analysis_cancel.is_set():
                    self.session.set_auto(key, stops)
            except CancelledError:
                pass
            except Exception as exc:
                if not self._analysis_cancel.is_set():
                    traceback.print_exc()
                    if self.session.auto_key() == key:
                        self.auto_error = f"auto exposure could not be measured ({_message(exc)})"
            if (
                self.session.auto_pending
                and self.measure_due is None
                and not self.playing
                and not self.scrubbing
            ):
                self.measure_due = time.monotonic() + SETTLE_S
            changed = waiting and (not self.session.auto_pending or self.auto_error is not None)
            if (
                changed and not self.playing and self.shown is not None and self.shown[0] == self.newest
            ):  # show the measured value
                try:
                    self._draft(self.shown[0], time.perf_counter())
                except Exception as exc:
                    self._draft_revision += 1
                    self._send(_error(self.shown[0], _code(exc), _message(exc)), self.shown[0])
        self._measure()
        if self.refining is not None and self.refining[0].done():
            task, seq, index, started = self.refining
            self.refining = None
            try:
                refined = task.result()
            except Exception as exc:
                if self._refining_revision == self._draft_revision:
                    self._send(_error(seq, _code(exc), _message(exc)), seq)
            else:
                if self._refining_revision == self._draft_revision:
                    header = self._header(seq, "refined", started) | {"index": index, "notes": refined.notes}
                    header |= {"width": self.session.size[0], "height": self.session.size[1]}
                    self._send((header, refined.jpeg), seq)
        if self.refine_due is not None and self.refining is None and time.monotonic() >= self.refine_due[0]:
            _, seq, index = self.refine_due
            self.refine_due = None
            if seq == self.newest:
                try:
                    refine = self.session.refiner(index)
                except Exception as exc:
                    self._send(_error(seq, _code(exc), _message(exc)), seq)
                    return
                self._refining_revision = self._draft_revision
                self.refining = (self.background.submit(refine), seq, index, time.perf_counter())
