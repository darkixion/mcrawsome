"""Publish open-handle state off the FUSE request path, without decoding or scanning processes."""

from __future__ import annotations

import json
import logging
import os
import threading
from contextlib import suppress
from pathlib import Path

_LOG = logging.getLogger(__name__)


class UsageReporter:
    def __init__(self, folder: Path, start: str | None) -> None:
        self.folder = folder
        self.start = start
        self.condition = threading.Condition()
        self.readers = 0
        self.stopped = False
        self._publish(False)
        self.worker = threading.Thread(target=self._run, name="media-use", daemon=True)
        self.worker.start()

    def opened(self) -> None:
        with self.condition:
            self.readers += 1
            self.condition.notify()

    def released(self) -> None:
        with self.condition:
            self.readers = max(0, self.readers - 1)
            self.condition.notify()

    def _publish(self, in_use: bool) -> None:
        target = self.folder / "usage.json"
        try:
            temporary = self.folder / "usage.part"
            temporary.write_text(json.dumps({"pid": os.getpid(), "start": self.start, "in_use": in_use}))
            temporary.replace(target)
        except OSError:
            # Reporting must not interrupt media reads. Missing state is reported as unknown.
            with suppress(OSError):
                target.unlink(missing_ok=True)
            _LOG.exception("Could not publish media use")

    def _run(self) -> None:
        previous = False
        while True:
            with self.condition:
                while not self.stopped and bool(self.readers) == previous:
                    self.condition.wait()
                if self.stopped:
                    return
                current = bool(self.readers)
            self._publish(current)
            previous = current

    def close(self) -> None:
        with self.condition:
            self.stopped = True
            self.condition.notify()
        self.worker.join(timeout=1)
