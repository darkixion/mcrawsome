"""Scheduling of background interface work."""

import contextlib
import os
import sys
import threading


def background_priority() -> None:
    """On Linux, lower only this thread's priority; its render children inherit it.

    The next draft and the interface keep their priority while analysis and refinement run.
    Other platforms keep the scheduler's defaults.
    """
    if sys.platform == "linux":
        tid = threading.get_native_id()
        with contextlib.suppress(OSError):
            os.setpriority(os.PRIO_PROCESS, tid, max(10, os.getpriority(os.PRIO_PROCESS, tid)))
