"""Shared limits for lightweight mount services and their expensive work."""

import fcntl
import os
import threading
import time
from collections.abc import Iterator
from concurrent.futures import CancelledError
from contextlib import contextmanager
from pathlib import Path

MAX_MOUNTS = 64
WORKERS = 4
# Readers interleave chunks from neighbouring sensor frames; keep the playback working set. Resolve reads
# eight frames at once: when they don't all fit, a frame is decoded again for nearly every chunk.
CACHE_BYTES = 128 << 20
CACHE_FRAMES = 12  # served frames kept at least, however large
# A mount lives as long as an editor's project uses it, mostly unread: once reads have stopped this long, it
# lets go of the frames it decoded and gives the memory back. Reading again decodes them as before.
IDLE_RELEASE_S = 30.0
IDLE_CHECK_S = 5.0


@contextmanager
def work_slot(folder: Path, stop: threading.Event | None = None) -> Iterator[None]:
    """Share four cross-process slots between startup and RAW decompression."""
    slots = [os.open(folder / f"decode-{i}.lock", os.O_CREAT | os.O_RDWR, 0o600) for i in range(WORKERS)]
    try:
        while True:
            if stop is not None and stop.is_set():
                raise CancelledError
            for slot in slots:
                try:
                    fcntl.flock(slot, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue
                try:
                    yield
                finally:
                    fcntl.flock(slot, fcntl.LOCK_UN)
                return
            if stop is not None:
                stop.wait(0.005)
            else:
                time.sleep(0.005)
    finally:
        for slot in slots:
            os.close(slot)
