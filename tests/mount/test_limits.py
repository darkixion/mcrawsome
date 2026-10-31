import threading
from concurrent.futures import CancelledError, ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path

import pytest

from mcrawsome.mount.limits import WORKERS, work_slot


def test_independent_threads_share_decode_slots_and_waiting_work_is_cancellable(tmp_path: Path) -> None:
    # Separate opens are essential: flock on a reused descriptor grants a second thread its own lock.
    with ExitStack() as stack, ThreadPoolExecutor(max_workers=1) as pool:
        for _ in range(WORKERS):
            stack.enter_context(work_slot(tmp_path))
        started, stop = threading.Event(), threading.Event()

        def wait_for_slot() -> None:
            started.set()
            with work_slot(tmp_path, stop):
                pytest.fail("a fifth decoder obtained a shared slot")

        future = pool.submit(wait_for_slot)
        assert started.wait(2)
        stop.set()
        with pytest.raises(CancelledError):
            future.result(timeout=2)
    with work_slot(tmp_path):
        pass  # cancellation and normal exit released every descriptor/lock
