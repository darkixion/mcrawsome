"""One filesystem process. The parent engine preserves it until an explicit unmount."""

import ctypes
import json
import os
import sys
import threading
import time
from contextlib import suppress
from pathlib import Path

import numpy as np
import numpy.typing as npt

from mcrawsome import _fuse
from mcrawsome.io.clip import Clip
from mcrawsome.mount.limits import CACHE_BYTES, CACHE_FRAMES, IDLE_CHECK_S, IDLE_RELEASE_S, work_slot
from mcrawsome.mount.manager import MountedSequence, process_start
from mcrawsome.mount.readahead import FrameReadAhead
from mcrawsome.mount.runtime import running_runtime
from mcrawsome.mount.sequence import Sequence, source_stamp
from mcrawsome.mount.usage import UsageReporter


def trim_heap() -> None:
    """Give freed memory back to the system: glibc otherwise keeps large freed blocks for later."""
    with suppress(AttributeError, OSError):
        ctypes.CDLL(None).malloc_trim(0)


class ServingSequence(Sequence):
    def __init__(self, record: MountedSequence, folder: Path) -> None:
        if source_stamp(record.source) != record.stamp:
            raise ValueError("source changed before mounting")
        super().__init__(
            record.source,
            cache_bytes=CACHE_BYTES,
            vignette_profile=record.vignette_profile,
            raw_settings=record.raw_settings,
            frame_range=record.frame_range,
            quad_bayer_remosaic=record.quad_bayer_remosaic,
            colour_matrix=record.colour_matrix,
            timecode=record.timecode,
        )
        first = self.clip.frame_metadata(0)
        _, pixels = self._header(self.sources[0])  # a served frame's pixel bytes
        self.limit = max(self.limit, first.width * first.height * 2, CACHE_FRAMES * pixels)
        self.folder = folder
        self.usage = UsageReporter(folder, process_start(os.getpid()))
        self._ahead_clips: dict[int, Clip] = {}  # each read-ahead thread's own reader
        self._ahead_lock = threading.Lock()
        self._read_ahead = FrameReadAhead(self.clip.frame_count, self._decode_demand, self._decode_ahead)
        self._serving = threading.Lock()  # a read, or letting go of frames once reads have stopped
        self._read_at = time.monotonic()
        self._holding = False  # frames may be cached or decoded ahead
        self._closing = threading.Event()
        self._idle = threading.Thread(target=self._release_when_idle, name="mount-idle", daemon=True)
        self._idle.start()

    def read(self, name: str, offset: int, size: int) -> bytes | memoryview:
        with self._serving:
            self._read_at, self._holding = time.monotonic(), True
            return super().read(name, offset, size)

    def release_if_idle(self, now: float) -> bool:
        """Let go of the decoded frames once reads have stopped for IDLE_RELEASE_S. True if it did."""
        with self._serving:
            if not self._holding or now - self._read_at < IDLE_RELEASE_S:
                return False
            self.pixels.clear()
            self.cached_bytes = 0
            self._read_ahead.drop()
            self._holding = False
        trim_heap()
        return True

    def _release_when_idle(self) -> None:
        while not self._closing.wait(IDLE_CHECK_S):
            self.release_if_idle(time.monotonic())

    def _decode(self, index: int, factor: int | None = None) -> npt.NDArray[np.uint16]:
        if factor is not None and factor != self.factor:  # the full-size first frame: read once, directly
            with work_slot(self.folder.parent):
                return super()._decode(index, factor)
        return self._read_ahead.read(index)

    def _decode_demand(self, index: int) -> npt.NDArray[np.uint16]:
        with work_slot(self.folder.parent):
            return super()._decode(index)

    def _decode_ahead(self, index: int, stop: threading.Event) -> npt.NDArray[np.uint16]:
        # Each operation opens its own lock descriptors: flock on a shared descriptor would
        # let two threads claim the same slot. Demand and read-ahead still share four slots.
        with work_slot(self.folder.parent, stop):
            self._check()
            return self._pixels(self._ahead_reader().raw_frame(index), self.factor)

    def _ahead_reader(self) -> Clip:
        """This thread's reader: a reader's file position and decoder belong to one thread at a time."""
        thread = threading.get_ident()
        with self._ahead_lock:
            clip = self._ahead_clips.get(thread)
            if clip is None:
                clip = self._ahead_clips[thread] = self.clip.independent_reader()
            return clip

    def opened(self) -> None:
        self.usage.opened()

    def released(self) -> None:
        self.usage.released()

    def close(self) -> None:
        if hasattr(self, "_idle"):
            self._closing.set()
            self._idle.join()
        if hasattr(self, "_read_ahead"):
            self._read_ahead.close()  # its workers have stopped: nothing uses their readers now
        clips = getattr(self, "_ahead_clips", {})
        for clip in clips.values():
            clip.close()
        clips.clear()
        if hasattr(self, "usage"):
            self.usage.close()
        super().close()

    def ready(self) -> None:
        ready = self.folder / "ready.json"
        temporary = self.folder / "ready.part"
        temporary.write_text(
            json.dumps(
                {
                    "frames": len(self.sources),
                    "pid": os.getpid(),
                    "start": process_start(os.getpid()),
                    "runtime": running_runtime(),
                }
            )
        )
        temporary.replace(ready)


def serve(folder: Path) -> None:
    """Serve the sequence recorded in `folder` until it is unmounted."""
    record = MountedSequence.model_validate_json((folder / "sequence.json").read_bytes())
    with ServingSequence(record, folder) as sequence:
        _fuse.serve(sequence, str(record.path))


def main() -> None:
    serve(Path(sys.argv[1]))


if __name__ == "__main__":
    main()
