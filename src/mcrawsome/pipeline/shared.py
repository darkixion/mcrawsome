"""Decoded video and developed RAW frames for the worker pool, without pickling them.

One FFmpeg process decodes in the parent; each frame goes into a slot of one shared-memory block, and a
task carries only (frame index, slot). RAW develop tasks fill float32 slots in the workers.
Memory is bounded by the number of slots.
"""

from __future__ import annotations

import itertools
import os
import subprocess
import sys
from io import BufferedReader
from multiprocessing import shared_memory
from pathlib import Path
from types import TracebackType
from typing import Self, cast, overload

import numpy as np
import numpy.typing as npt

from mcrawsome.io.vaapi import hwaccel

Frame = npt.NDArray[np.uint16]
_FLAGS = "accurate_rnd+full_chroma_int"
_numbers = itertools.count()


def block_prefix(pid: int) -> str:
    """The start of the names of the shared-memory blocks process `pid` makes (frame rings): a process
    stopped while it held one cannot remove it, so whoever stopped it can find it by this name."""
    return f"mcrawsome_{pid}_"


def _create_block(size: int) -> shared_memory.SharedMemory:
    """A new block named mcrawsome_<pid>_<n>, so that the blocks of one process can be told from those of
    other processes (well within the 31 characters macOS allows). A name left behind by an earlier process
    with the same pid is skipped."""
    while True:
        try:
            name = f"{block_prefix(os.getpid())}{next(_numbers)}"
            return shared_memory.SharedMemory(name=name, create=True, size=size)
        except FileExistsError:
            continue


class FrameRing[T: np.generic]:
    @overload
    def __init__(self: FrameRing[np.uint16], slots: int, shape: tuple[int, int, int]) -> None: ...

    @overload
    def __init__(self, slots: int, shape: tuple[int, int, int], dtype: type[T]) -> None: ...

    def __init__(self, slots: int, shape: tuple[int, int, int], dtype: type[np.generic] = np.uint16) -> None:
        self.slots, self.shape = slots, shape
        self.dtype = np.dtype(dtype)
        self.frame_bytes = int(np.prod(shape)) * self.dtype.itemsize
        self._shm = _create_block(self.frame_bytes * slots)
        self.name = self._shm.name

    def view(self, slot: int) -> npt.NDArray[T]:
        start = slot * self.frame_bytes
        buf = self._shm.buf
        assert buf is not None
        return np.ndarray(self.shape, dtype=self.dtype, buffer=buf[start : start + self.frame_bytes])

    def close(self) -> None:
        self._shm.close()
        self._shm.unlink()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


_attached: dict[str, shared_memory.SharedMemory] = {}


def _attach(name: str) -> shared_memory.SharedMemory:
    """Attach without taking ownership: only the parent's FrameRing may unlink the block.

    Before Python 3.13 attaching also registers the block with the resource tracker. Workers started
    by the parent share its tracker, which keeps a set of names, so that registration changes nothing
    and must not be undone: unregistering here would remove the parent's entry, and the parent's
    unlink would then make the tracker print a KeyError.
    """
    if sys.version_info >= (3, 13):
        return shared_memory.SharedMemory(name=name, track=False)
    return shared_memory.SharedMemory(name=name)


def slot_view[T: np.generic](
    name: str, slot: int, shape: tuple[int, int, int], dtype: type[T]
) -> npt.NDArray[T]:
    """A worker's view of a slot. The parent keeps it allocated until the task has finished."""
    if name not in _attached:
        _attached[name] = _attach(name)
    size = int(np.prod(shape)) * np.dtype(dtype).itemsize
    buf = _attached[name].buf
    assert buf is not None
    return np.ndarray(shape, dtype=dtype, buffer=buf[slot * size : (slot + 1) * size])


def read_slot(name: str, slot: int, shape: tuple[int, int, int]) -> Frame:
    """A copy of one slot, from a worker process (the block is attached once per worker)."""
    return slot_view(name, slot, shape, np.uint16).copy()


class VideoDecoder:
    """Frames of a video as 16-bit RGB (full range, the colour matrix it was recorded with), from the first
    frame at or after `start_s` seconds. With a GPU `device` (mcrawsome.io.vaapi: a VAAPI render node, or
    an NVIDIA GPU as cuda:N) the GPU's video engine decodes and FFmpeg downloads each frame; FFmpeg decodes
    a stream the device cannot itself. `hardware_scale` scales on a VAAPI device before the download; an
    NVIDIA GPU's frames are scaled on the processor, as the AppImage's FFmpeg has no CUDA filters."""

    def __init__(
        self,
        ffmpeg: Path,
        video: Path,
        size: tuple[int, int],
        matrix: str = "bt709",
        start_s: float = 0.0,
        device: str | None = None,
        threads: int | None = None,
        codec: str | None = None,
        hardware_scale: bool = False,
    ) -> None:
        self.size = size
        self._bytes = size[0] * size[1] * 6
        hardware = hwaccel(device) if device else []
        # libdav1d is software-only. FFmpeg's native AV1 decoder decodes on the GPU (VAAPI or NVDEC).
        decoder = ["-c:v", "av1"] if device and codec == "av1" else []
        seek = ["-ss", f"{start_s:.6f}"] if start_s > 0 else []
        threading = ["-threads", str(threads), "-filter_threads", "1"] if threads else []
        scale = f"scale=w={size[0]}:h={size[1]}:in_color_matrix={matrix}:out_range=full:flags={_FLAGS}"
        if device and hardware_scale and not device.startswith("cuda:"):
            hardware += ["-hwaccel_output_format", "vaapi"]
            scale = f"scale_vaapi=w={size[0]}:h={size[1]}:format=p010le,hwdownload,format=p010le,{scale}"
        self._proc = subprocess.Popen(
            [str(ffmpeg), "-nostdin", "-v", "error", "-noautorotate", *threading, *hardware, *decoder, *seek,
             "-i", str(video),
             "-map", "0:v:0",
             "-vf", f"{scale},format=rgb48le",
             "-fps_mode", "passthrough", "-f", "rawvideo", "-"],
            stdout=subprocess.PIPE,
        )  # fmt: skip
        assert self._proc.stdout is not None
        self._stdout = cast(BufferedReader, self._proc.stdout)

    def read_into(self, target: Frame) -> bool:
        """Fill `target` with the next frame; False at the end of the video."""
        view = memoryview(target.reshape(-1).view(np.uint8))
        got = 0
        while got < self._bytes:
            n = self._stdout.readinto(view[got:])
            if not n:
                return False
            got += n
        return True

    def close(self) -> None:
        if self._proc.poll() is None:
            self._proc.kill()
        self._proc.wait()
        if self._proc.stdout:
            self._proc.stdout.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        self.close()
