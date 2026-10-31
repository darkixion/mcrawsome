"""Bounded fast-frame work: one GPU owner, or two independent CPU lanes."""

from __future__ import annotations

import contextlib
import math
import queue
import threading
import time
from collections import deque
from collections.abc import Callable
from concurrent.futures import CancelledError, Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import TYPE_CHECKING

from mcrawsome.pipeline.priority import background_priority
from mcrawsome.preview.backend import gpu_enabled
from mcrawsome.preview.display import DisplayFrames

if TYPE_CHECKING:
    from mcrawsome.preview.session import PreviewSession, Refined


@dataclass(frozen=True)
class Draft:
    frame: Refined
    timings: dict[str, float]
    backend: str
    reason: str


class _Lane:
    def __init__(self, *, prefetch: bool = False, foreground: bool = False) -> None:
        self.prefetch = prefetch
        self.executor = ThreadPoolExecutor(
            1,
            thread_name_prefix="preview-device" if foreground else "preview-ahead",
            initializer=None if foreground else background_priority,
        )
        self.session: PreviewSession | None = None
        self.task: Future[Draft] | None = None
        self.cancelled = threading.Event()

    def run(self, snapshot: PreviewSession, index: int, width: int, drag: bool = False) -> Draft:
        session = self.adopt(snapshot)
        frame = session.fast(index, width, drag=drag, prefetch=self.prefetch)
        session.backend.check_cancelled()
        return Draft(frame, dict(session.fast_timings), session.backend.name, session.backend.reason)

    def adopt(self, snapshot: PreviewSession) -> PreviewSession:
        cancelled = snapshot.backend.cancelled
        if self.session is None:
            self.session = snapshot.fork_fast()
        else:
            # Only this lane's thread touches its reader, calibration and device. The snapshot
            # carries immutable settings and cache generations captured on the request thread.
            self.session.adopt_fast(snapshot)
        session = self.session
        stopped = self.cancelled
        session.backend.cancelled = lambda: stopped.is_set() or (cancelled is not None and cancelled())
        session.backend.check_cancelled()
        return session

    def source(
        self,
        snapshot: PreviewSession,
        index: int,
        display: DisplayFrames | None = None,
        width: int | None = None,
    ) -> Callable[[], Draft]:
        """Develop frame `index` on this lane now (every photosite, or a draft `width` wide); the returned
        call does the rest (finishing on the CPU, encoding or the display copy) on another thread."""
        session = self.adopt(snapshot)
        encode = (
            session.source_encoder(index, prefetch=True, display=display)
            if width is None
            else session.draft_encoder(index, width, prefetch=True, display=display)
        )
        timings, backend, reason = session.fast_timings, session.backend.name, session.backend.reason

        def finish() -> Draft:
            frame = encode()
            try:
                session.backend.check_cancelled()
            except BaseException:
                frame.discard()
                raise
            return Draft(frame, dict(timings), backend, reason)

        return finish

    def close(self) -> None:
        self.executor.shutdown(wait=True, cancel_futures=True)
        if self.session is not None:
            self.session.close()


class FastPipeline:
    def __init__(self, owner: PreviewSession) -> None:
        self.owner = owner
        self.serial_gpu = owner.settings.gpu and gpu_enabled()
        self.lanes = [_Lane(prefetch=True, foreground=True)] if self.serial_gpu else [_Lane(), _Lane()]
        self.streaming: PlaybackStream | None = None

    def prepare(self) -> None:
        if self.serial_gpu:
            lane = self.lanes[0]
            if lane.session is None:
                lane.session = self.owner.fork_fast()
                lane.session.backend.prepare()

    def render(self, index: int, width: int, *, drag: bool = False) -> Draft:
        """Use the GPU's one owner, yielding any speculative frame before foreground work."""
        self.stop_stream()
        lane = self.lanes[0]
        if lane.task is not None:
            lane.cancelled.set()
            with contextlib.suppress(Exception):
                lane.task.result()
        lane.cancelled = threading.Event()
        lane.task = lane.executor.submit(lane.run, self.owner.fork_fast(), index, width, drag)
        return lane.task.result()

    def _source_lane(self) -> _Lane:
        self.cancel()
        self.stop_stream()
        for lane in self.lanes:
            if lane.task is not None:
                with contextlib.suppress(Exception):
                    lane.task.result()
        lane = self.lanes[0]
        lane.cancelled = threading.Event()
        return lane

    def render_source(self, index: int) -> Draft:
        lane = self._source_lane()
        snapshot = self.owner.fork_fast()

        def render() -> Draft:
            return lane.source(snapshot, index)()

        lane.task = lane.executor.submit(render)
        return lane.task.result()

    def stream(
        self,
        index: int,
        *,
        display: DisplayFrames | None = None,
        width: int | None = None,
    ) -> PlaybackStream:
        """Play from `index` at source resolution, or as drafts `width` wide."""
        lane = self._source_lane()
        stream = PlaybackStream(lane, self.owner.fork_fast(), index, display, width)
        self.streaming = stream
        stream.task = lane.executor.submit(stream.produce)
        return stream

    def stop_stream(self) -> None:
        stream, self.streaming = self.streaming, None
        if stream is not None:
            stream.cancel()
            if stream.task is not None:
                with contextlib.suppress(Exception):
                    stream.task.result()

    @property
    def in_flight(self) -> int:
        return sum(lane.task is not None and not lane.task.done() for lane in self.lanes)

    def submit(self, index: int, width: int) -> Future[Draft] | None:
        if self.streaming is not None:
            return None
        for lane in self.lanes:
            if lane.task is not None:
                if not lane.task.done():
                    continue
                try:
                    lane.task.result()  # observe speculative failures even when nobody awaits them
                except CancelledError:
                    pass
                except Exception:
                    # Foreground requests retry through their own backend and report their errors.
                    pass
            lane.cancelled = threading.Event()
            lane.task = lane.executor.submit(lane.run, self.owner.fork_fast(), index, width)
            return lane.task
        return None

    def cancel(self) -> None:
        """Signal the lanes, without waiting for a CPU stage or an already submitted GPU kernel."""
        if self.streaming is not None:
            self.streaming.cancel()
        for lane in self.lanes:
            lane.cancelled.set()
            if lane.task is not None:
                lane.task.cancel()

    def close(self) -> None:
        self.cancel()
        self.stop_stream()
        for lane in self.lanes:
            lane.close()


class PlaybackStream:
    """One device owner and one encoder, with a bounded window ahead of the audio clock."""

    def __init__(
        self,
        lane: _Lane,
        snapshot: PreviewSession,
        index: int,
        display: DisplayFrames | None = None,
        width: int | None = None,
    ) -> None:
        self.lane, self.snapshot = lane, snapshot
        self.display, self.width = display, width
        self.stopped = lane.cancelled
        self.clock = index
        self.lead = 0
        # Drafts are small: more of them ahead absorb a stall under load. Memory bounds Source frames.
        self.base_window = 3 if width is None else 6
        self.window = self.base_window
        self._start = index
        self._changed = threading.Condition()
        self._output: queue.Queue[tuple[int, Draft]] = queue.Queue(maxsize=2)
        self.task: Future[None] | None = None
        self.failure: BaseException | None = None

    def advance(self, index: int) -> None:
        with self._changed:
            self.clock = max(self.clock, min(self.snapshot.frame_count - 1, index))
            self._changed.notify_all()

    def pace(self, index: int, periods: float) -> None:
        """After frame `index` took `periods` frame periods: aim further ahead of the clock when a slow frame
        was done too late to be shown on time, leaving time for transport and decode. A slow frame done
        before the clock reached it started early enough, so it never moves the aim ahead (which would skip
        frames the window had time for); it may only bring it back."""
        with self._changed:
            if periods <= 1:
                self.lead = 0
            else:
                wanted = min(max(4, round(self.snapshot.rate / 2)), math.ceil(periods) + 1)
                self.lead = wanted if index <= self.clock else min(self.lead, wanted)
            self.window = max(self.base_window, self.lead + 2)

    def cancel(self) -> None:
        self.stopped.set()
        while True:
            try:
                self._output.get_nowait()[1].frame.discard()
            except queue.Empty:
                break
        with self._changed:
            self._changed.notify_all()

    def receive(self, timeout: float = 0) -> tuple[int, Draft] | None:
        deadline = time.monotonic() + timeout
        while not self.stopped.is_set():
            try:
                frame = self._output.get(timeout=min(0.02, max(0.0, deadline - time.monotonic())))
                # A completed picture still advances the display when the decoder cannot
                # sustain the audio rate. Discarding every late picture freezes the video.
                return frame
            except queue.Empty:
                if self.task is not None and self.task.done():
                    self.task.result()
                    return None
                if time.monotonic() >= deadline:
                    return None
        if self.failure is not None:
            raise self.failure
        return None

    def produce(self) -> None:
        pending: deque[Future[None]] = deque()
        index = self._start

        def publish(frame_index: int, encode: Callable[[], Draft]) -> None:
            draft = encode()
            self.pace(frame_index, draft.timings.get("total_ms", 0) * self.snapshot.rate / 1000)
            while not self.stopped.is_set():
                try:
                    self._output.put((frame_index, draft), timeout=0.02)
                    if self.stopped.is_set():
                        self.cancel()
                    return
                except queue.Full:
                    pass
            draft.frame.discard()

        with ThreadPoolExecutor(1, thread_name_prefix="preview-jpeg") as encoder:
            try:
                while not self.stopped.is_set():
                    while pending and pending[0].done():
                        pending.popleft().result()
                    if len(pending) == 2:
                        pending.popleft().result()
                        continue
                    with self._changed:
                        target = min(self.snapshot.frame_count - 1, self.clock + self.lead)
                        index = max(index, target)
                        available = index < min(self.snapshot.frame_count, self.clock + self.window)
                        if not available and not pending:
                            if index >= self.snapshot.frame_count:
                                return
                            self._changed.wait(0.02)
                            continue
                    if available:
                        encode = self.lane.source(self.snapshot, index, self.display, self.width)
                        pending.append(encoder.submit(publish, index, encode))
                        index += 1
                        continue
                    pending.popleft().result()
            except BaseException as exc:
                # A failure that stops the stream is kept before cancel() sets stopped: the task only ends
                # after this thread's teardown, and receive() returns at stopped, so it must find the
                # failure there rather than an end. Errors after a stop (a cancelled encode) are not one.
                if not self.stopped.is_set():
                    self.failure = exc
                self.cancel()
                raise
            finally:
                for future in pending:
                    future.cancel()
