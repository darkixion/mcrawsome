"""The stages of a long request, for the page that is waiting on it.

Each open event stream subscribes; a request publishes its stages to all of them, tagged with the token
the page sent with it. A stream that falls behind loses its oldest stages: only the latest one matters.
"""

from __future__ import annotations

import queue
import threading
from contextlib import suppress
from typing import Any

MAX_PENDING = 64

Stages = queue.Queue[dict[str, Any]]


class ProgressHub:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: set[Stages] = set()

    def subscribe(self) -> Stages:
        stages: Stages = queue.Queue(maxsize=MAX_PENDING)
        with self._lock:
            self._subscribers.add(stages)
        return stages

    def unsubscribe(self, stages: Stages) -> None:
        with self._lock:
            self._subscribers.discard(stages)

    def publish(self, stage: dict[str, Any]) -> None:
        with self._lock:
            for stages in self._subscribers:
                while True:
                    try:
                        stages.put_nowait(stage)
                        break
                    except queue.Full:
                        with suppress(queue.Empty):
                            stages.get_nowait()  # make room by dropping the oldest
