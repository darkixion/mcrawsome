"""Background preview work leaves CPU time for the next draft."""

import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from mcrawsome.pipeline.priority import background_priority


@pytest.mark.skipif(sys.platform != "linux", reason="Linux thread priorities")
def test_background_priority_is_local_to_its_thread() -> None:
    owner = threading.get_native_id()
    before = os.getpriority(os.PRIO_PROCESS, owner)

    def priority() -> tuple[int, int]:
        child = subprocess.check_output(
            [sys.executable, "-c", "import os; print(os.getpriority(os.PRIO_PROCESS, 0))"], text=True
        )
        return os.getpriority(os.PRIO_PROCESS, threading.get_native_id()), int(child)

    with ThreadPoolExecutor(1, initializer=background_priority) as pool:
        thread, child = pool.submit(priority).result()
        assert thread == child
        assert thread >= max(10, before)
    assert os.getpriority(os.PRIO_PROCESS, owner) == before
