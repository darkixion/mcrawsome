"""Ordered parallel map with a fixed number of items in flight (bounded memory)."""

from __future__ import annotations

import multiprocessing as mp
import signal
import threading
import time
import traceback
from collections import deque
from collections.abc import Callable, Generator, Iterable, Sequence
from multiprocessing.connection import wait
from multiprocessing.pool import AsyncResult, Pool
from multiprocessing.process import BaseProcess
from typing import Any

SETTLE_S = 120.0  # how long an early stop waits for the tasks in flight
POLL_S = 0.25  # how often a wait for a result checks that the workers are alive
RELEASE_S = 10.0  # how long closing a pool waits for a worker to let go of the task queue


class WorkerLostError(RuntimeError):
    """A worker stopped without finishing its task. Pool replaces the worker but never the task's result,
    so a wait for it would never end."""


def ignore_ctrl_c() -> None:
    """Pool initialiser: Ctrl+C reaches only the main process, which stops the render and the pool, so
    workers finish the tasks in flight instead of dying with them."""
    signal.signal(signal.SIGINT, signal.SIG_IGN)


class _WorkerPool(Pool):
    """Pool, closing after a worker died waiting for a task. One killed while it sends a result, holding
    the result queue's lock, still leaves Pool.terminate waiting for that lock; results are packed before
    the lock is taken, so a worker rarely dies holding it."""

    def terminate(self) -> None:
        super().terminate()
        # A task a dead worker lost never finishes, so its result and the pool keep each other (Pool lets go
        # when a result arrives). Left to the garbage collector, the pool's semaphores can be freed while the
        # resource tracker is busy, which refuses them: see result_of.
        cache = self._cache  # type: ignore[attr-defined]
        for result in cache.values():
            result._pool = None
        cache.clear()

    @staticmethod
    def _help_stuff_finish(inqueue: Any, task_handler: threading.Thread, size: int) -> None:
        """As Pool's own: take the task queue's lock, so no worker takes another task, and empty the queue,
        so the task handler is not left waiting to add to it. A worker holds that lock while it waits for a
        task, and one killed then never lets go, so Pool's own would wait for ever. No live worker can read
        the queue without the lock either, so after a while it is emptied without it."""
        inqueue._rlock.acquire(timeout=RELEASE_S)
        while task_handler.is_alive() and inqueue._reader.poll():
            inqueue._reader.recv_bytes()
            time.sleep(0)


def worker_pool(workers: int) -> Pool:
    """Spawned workers that leave Ctrl+C to the main process. Every pool used with bounded_imap must be one
    of these: a worker killed by Ctrl+C never answers, and settle would wait for it in vain."""
    return _WorkerPool(workers, initializer=ignore_ctrl_c, context=mp.get_context("spawn"))


def workers_of(pool: Pool) -> tuple[BaseProcess, ...]:
    """The pool's worker processes now, so a wait can tell later that one has died: Pool puts a new worker
    in a dead one's place in its own list. A thread pool's workers are threads, which don't vanish so."""
    return tuple(worker for worker in pool._pool if isinstance(worker, BaseProcess))  # type: ignore[attr-defined]


def _dead(workers: Sequence[BaseProcess]) -> list[BaseProcess]:
    # A worker's sentinel is ready once it has exited; waiting on it reaps nothing, unlike exitcode.
    ended = set(wait([worker.sentinel for worker in workers], timeout=0)) if workers else set()
    return [worker for worker in workers if worker.sentinel in ended]


def _lost(worker: BaseProcess) -> WorkerLostError:
    code, how, note = worker.exitcode, "", ""
    if code is not None and code < 0 and -code in signal.valid_signals():
        how = f" ({signal.Signals(-code).name})"
        if code == -signal.SIGKILL:
            note = " The system stops processes with SIGKILL when memory runs out."
    elif code is not None:
        how = f" (exit status {code})"
    return WorkerLostError(f"A worker process stopped unexpectedly{how}, so its task never finished.{note}")


def settle(
    results: Iterable[AsyncResult[Any]], timeout: float = SETTLE_S, workers: Sequence[BaseProcess] = ()
) -> None:
    """Wait for tasks still running before their pool is terminated. A worker killed while it sends a large
    result leaves half a message in the result pipe, and Pool.terminate then waits for the rest forever.
    A dead worker's task never finishes, so the wait also ends once no more tasks remain than `workers`
    (from workers_of) have died, and at most RELEASE_S after a death: a worker that died waiting for a
    task holds the task queue, and the tasks still in it never start."""
    deadline = time.monotonic() + timeout
    waiting = list(results)
    while (left := deadline - time.monotonic()) > 0:
        waiting = [result for result in waiting if not result.ready()]
        dead = _dead(workers)
        if len(waiting) <= len(dead):
            return
        if dead:
            deadline = min(deadline, time.monotonic() + RELEASE_S)
        waiting[0].wait(min(POLL_S, left))


def result_of[R](result: AsyncResult[R], workers: Sequence[BaseProcess] = ()) -> R:
    """result.get(), failing with WorkerLostError once one of `workers` (from workers_of) has died instead of
    waiting for ever, and leaving nothing to the garbage collector when the task failed.

    A failed task's AsyncResult holds its exception, whose traceback holds the frame of AsyncResult.get,
    which holds the AsyncResult. Every frame the exception passes through joins that cycle, and with them
    the pool, whose semaphores are freed only with it. Left to the garbage collector, they can be freed
    while the resource tracker is busy starting another pool, and the tracker refuses them ("called
    reentrantly ... might leak"). So the frames that have returned are cleared, and this frame lets go of
    the result. Callers must not keep the result in a variable either.
    """
    try:
        while not result.ready():
            result.wait(POLL_S)
            if not result.ready() and (dead := _dead(workers)):
                raise _lost(dead[0])
        return result.get()
    except BaseException as exc:
        del result
        traceback.clear_frames(exc.__traceback__)
        raise


def bounded_imap[T, R](
    pool: Pool, fn: Callable[[T], R], items: Iterable[T], window: int
) -> Generator[R, None, None]:
    workers = workers_of(pool)
    pending: deque[AsyncResult[R]] = deque()
    try:
        for item in items:
            pending.append(pool.apply_async(fn, (item,)))
            if len(pending) >= window:
                yield result_of(pending.popleft(), workers)
        while pending:
            yield result_of(pending.popleft(), workers)
    finally:  # stopped early or a task failed: let the others finish before the pool goes
        settle(pending, workers=workers)
