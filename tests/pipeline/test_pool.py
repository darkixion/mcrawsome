import ast
import gc
import multiprocessing as mp
import operator
import os
import signal
import time
import weakref
from collections.abc import Callable, Iterator
from functools import partial
from multiprocessing.pool import Pool
from pathlib import Path

import pytest

from mcrawsome.pipeline.pool import WorkerLostError, bounded_imap, result_of, settle, worker_pool, workers_of

# Built-in functions are used because "spawn" workers cannot import functions defined in test files.


def test_results_are_in_order() -> None:
    with mp.get_context("spawn").Pool(3) as pool:
        assert list(bounded_imap(pool, abs, range(-10, 10), window=4)) == [abs(x) for x in range(-10, 10)]


def test_never_more_than_window_in_flight() -> None:
    consumed: list[int] = []

    def items() -> Iterator[int]:
        for i in range(50):
            consumed.append(i)
            yield i

    with mp.get_context("spawn").Pool(2) as pool:
        it = bounded_imap(pool, abs, items(), window=5)
        next(it)
        assert len(consumed) <= 6


STOP_EARLY = """
import contextlib, multiprocessing as mp
from mcrawsome.pipeline.pool import bounded_imap
if __name__ == "__main__":
    for _ in range(30):
        with mp.get_context("spawn").Pool(4) as pool:
            with contextlib.closing(bounded_imap(pool, bytes, [4_000_000] * 40, window=8)) as results:
                next(results)
"""


def test_stopping_early_does_not_hang_the_pool() -> None:
    # Large results: a worker terminated while sending one used to leave Pool.terminate waiting forever.
    import subprocess
    import sys

    run = subprocess.run([sys.executable, "-c", STOP_EARLY], capture_output=True, text=True, timeout=120)
    assert run.returncode == 0, run.stderr


def test_every_pool_leaves_ctrl_c_to_the_main_process() -> None:
    # A worker killed by Ctrl+C never answers, and an early stop would wait for it.
    src = Path(__file__).parents[2] / "src" / "mcrawsome"
    makers = [
        path
        for path in src.rglob("*.py")
        if path.name != "pool.py"
        and any(
            isinstance(node, ast.Call)
            and (
                (isinstance(node.func, ast.Attribute) and node.func.attr == "Pool")
                or (isinstance(node.func, ast.Name) and node.func.id == "Pool")
            )
            for node in ast.walk(ast.parse(path.read_text()))
        )
    ]
    assert makers == []


def test_workers_survive_ctrl_c() -> None:
    import os
    import signal

    from mcrawsome.pipeline.pool import worker_pool

    with worker_pool(1) as pool:
        before = pool.apply(os.getpid)
        os.kill(before, signal.SIGINT)
        assert pool.apply(os.getpid) == before


def _fail_in_a_pool(pools: list[weakref.ref[Pool]]) -> None:
    with worker_pool(2) as pool:
        pools.append(weakref.ref(pool))
        list(bounded_imap(pool, int, ["not a number"] * 4, window=2))


def test_a_failed_task_does_not_leave_its_pool_to_the_garbage_collector() -> None:
    # The pool's semaphores are freed with the pool. Left for the collector, they could be freed while the
    # resource tracker is busy starting another pool, which refuses them ("ResourceTracker called
    # reentrantly ... might leak").
    pools: list[weakref.ref[Pool]] = []
    gc.collect()
    gc.disable()
    try:
        with pytest.raises(ValueError, match="not a number"):
            _fail_in_a_pool(pools)
        assert pools[0]() is None
    finally:
        gc.enable()


# multiprocessing.Pool replaces a worker that dies but never finishes the task it was running, so a wait for
# that task's result would never end. These fail fast; their timeouts stop a regression stalling the gate.
# Each task is a partial of a built-in function, which call runs in a worker.
call: Callable[[Callable[[], object]], object] = operator.call


@pytest.mark.timeout(60)
def test_a_task_whose_worker_dies_fails_instead_of_waiting_for_ever() -> None:
    with worker_pool(1) as pool:
        processes = workers_of(pool)
        pid = pool.apply(os.getpid)
        lost = pool.apply_async(os.kill, (pid, signal.SIGKILL))  # the worker dies running its task
        started = time.monotonic()
        with pytest.raises(WorkerLostError, match="SIGKILL"):
            result_of(lost, processes)
        settle([lost], workers=processes)  # nothing else is running, so nothing to wait for
        assert time.monotonic() - started < 10


def _kill_the_waiting_worker(pool: Pool) -> None:
    """Kill a one-worker pool's worker as it waits for a task, holding the task queue's lock. Just after its
    result arrives it may still hold the result queue's lock, and killed then, Pool.terminate waits for that
    lock for ever (results are packed before the lock is taken, so a worker rarely dies holding it)."""
    pid = pool.apply(os.getpid)
    sending = pool._outqueue._wlock  # type: ignore[attr-defined]
    assert sending.acquire(timeout=10)
    sending.release()
    time.sleep(0.2)  # into its wait for the next task
    os.kill(pid, signal.SIGKILL)


@pytest.mark.timeout(60)
def test_a_pool_closes_after_a_worker_dies_waiting_for_a_task(monkeypatch: pytest.MonkeyPatch) -> None:
    # A worker waits for a task holding the task queue's lock, which dies with it: nothing more is taken from
    # the queue, and Pool.terminate used to wait for that lock for ever.
    from mcrawsome.pipeline import pool as module

    monkeypatch.setattr(module, "RELEASE_S", 1.0)
    with worker_pool(1) as pool:
        processes = workers_of(pool)
        _kill_the_waiting_worker(pool)
        with pytest.raises(WorkerLostError, match="SIGKILL"):
            result_of(pool.apply_async(abs, (-1,)), processes)


@pytest.mark.timeout(60)
def test_a_map_stops_when_a_worker_dies() -> None:
    tasks: list[Callable[[], object]] = [
        partial(time.sleep, 0.2),
        partial(os._exit, 3),
        partial(time.sleep, 0.2),
    ]
    with worker_pool(2) as pool, pytest.raises(WorkerLostError, match="exit status 3"):
        list(bounded_imap(pool, call, tasks, window=3))


@pytest.mark.timeout(60)
def test_a_failed_map_does_not_wait_for_the_task_a_dead_worker_lost() -> None:
    # Settling used to wait SETTLE_S for a task that would never finish.
    tasks: list[Callable[[], object]] = [partial(time.sleep, -1), partial(os._exit, 3)]
    started = time.monotonic()
    with worker_pool(2) as pool, pytest.raises(ValueError, match="non-negative"):
        list(bounded_imap(pool, call, tasks, window=2))
    assert time.monotonic() - started < 20


def _lose_a_task(pools: list[weakref.ref[Pool]]) -> None:
    with worker_pool(2) as pool:
        pools.append(weakref.ref(pool))
        tasks: list[Callable[[], object]] = [partial(time.sleep, 0.2), partial(os._exit, 3)]
        list(bounded_imap(pool, call, tasks, window=2))


@pytest.mark.timeout(60)
def test_a_pool_that_lost_a_task_is_not_left_to_the_garbage_collector() -> None:
    # The lost task's result and the pool keep each other: see test_a_failed_task_does_not_leave_its_pool_...
    pools: list[weakref.ref[Pool]] = []
    gc.collect()
    gc.disable()
    try:
        with pytest.raises(WorkerLostError):
            _lose_a_task(pools)
        assert pools[0]() is None
    finally:
        gc.enable()


@pytest.mark.timeout(60)
def test_a_map_stops_soon_after_a_worker_dies_waiting_for_a_task(monkeypatch: pytest.MonkeyPatch) -> None:
    # The dead worker holds the task queue, so the tasks still queued never start: settling waits a little
    # for them, not SETTLE_S.
    from mcrawsome.pipeline import pool as module

    monkeypatch.setattr(module, "RELEASE_S", 1.0)
    started = time.monotonic()
    with worker_pool(1) as pool:
        _kill_the_waiting_worker(pool)
        with pytest.raises(WorkerLostError):
            list(bounded_imap(pool, abs, [-1, -2, -3, -4], window=4))
    assert time.monotonic() - started < 15
