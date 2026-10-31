"""Bounded local display slots, held until the native display has copied the frame out.

Slots hold four-byte BGRX pixels (the fourth byte is unused): GL uploads them without converting each pixel,
about twice as fast as three-byte BGR for a 4K frame. A lease says how many bytes its pixels have, so a
window can still show an older engine's three-byte frames."""

from __future__ import annotations

import mmap
import os
import secrets
import stat
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path
from types import TracebackType
from typing import Literal, Self

import numpy as np
import numpy.typing as npt
from pydantic import BaseModel, ConfigDict, Field, model_validator

_FOLDER = Path("/dev/shm")


class DisplayLease(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    name: str = Field(pattern=r"^mcrawsome-preview-[0-9]+-[a-zA-Z0-9_-]+$")
    slot: int = Field(ge=0, lt=5)
    token: str = Field(pattern=r"^[0-9a-f]{32}$")
    width: int = Field(ge=1, le=16384)
    height: int = Field(ge=1, le=16384)
    stride: int = Field(gt=0, le=64 * 1024**2 * 4)
    slots: int = Field(ge=1, le=5)
    channels: Literal[3, 4] = 3  # bytes per pixel: BGR from engines before BGRX slots, or BGRX

    @model_validator(mode="after")
    def bounds(self) -> Self:
        if self.slot >= self.slots or self.width * self.height * self.channels > self.stride:
            raise ValueError("invalid display frame bounds")
        return self


class DisplayFrames:
    def __init__(self, pixels: int, *, folder: Path = _FOLDER, slots: int = 5) -> None:
        if not 1 <= pixels <= 64 * 1024**2 or not 1 <= slots <= 5:
            raise ValueError("invalid display capacity")
        self.stride, self.slots = pixels * 4, slots
        descriptor, name = tempfile.mkstemp(prefix=f"mcrawsome-preview-{os.getpid()}-", dir=folder)
        self.path = Path(name)
        try:
            os.ftruncate(descriptor, self.stride * slots)
            self._mapping = mmap.mmap(descriptor, self.stride * slots)
        except BaseException:
            self.path.unlink(missing_ok=True)
            raise
        finally:
            os.close(descriptor)
        self._condition = threading.Condition()
        self._held: dict[int, str] = {}
        self._closed = False

    @property
    def in_use(self) -> int:
        with self._condition:
            return len(self._held)

    def write(self, pixels: npt.NDArray[np.uint8], check: Callable[[], None]) -> DisplayLease:
        if pixels.ndim != 3 or pixels.shape[2] != 3 or pixels.dtype != np.uint8:
            raise ValueError("display pixels must be BGR8")
        if pixels.shape[0] * pixels.shape[1] * 4 > self.stride:
            raise ValueError("display frame exceeds its slot")
        with self._condition:
            while True:
                check()
                if self._closed:
                    raise RuntimeError("display is closed")
                slot = next((i for i in range(self.slots) if i not in self._held), None)
                if slot is not None:
                    break
                self._condition.wait(0.02)
            token = secrets.token_hex(16)
            self._held[slot] = token
            try:
                shape = (pixels.shape[0], pixels.shape[1], 4)
                target = np.ndarray(shape, np.uint8, buffer=self._mapping, offset=slot * self.stride)
                _bgrx(pixels, target)
                del target
                check()
            except BaseException:
                del self._held[slot]
                self._condition.notify_all()
                raise
        return DisplayLease(
            name=self.path.name,
            slot=slot,
            token=token,
            width=pixels.shape[1],
            height=pixels.shape[0],
            stride=self.stride,
            slots=self.slots,
            channels=4,
        )

    def release(self, lease: DisplayLease) -> bool:
        with self._condition:
            if lease.name != self.path.name or self._held.get(lease.slot) != lease.token:
                return False
            del self._held[lease.slot]
            self._condition.notify_all()
            return True

    def close(self) -> None:
        with self._condition:
            if not self._closed:
                self._closed = True
                self._condition.notify_all()
                self._mapping.close()
                self.path.unlink(missing_ok=True)

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self, kind: type[BaseException] | None, error: BaseException | None, trace: TracebackType | None
    ) -> None:
        self.close()


def _bgrx(pixels: npt.NDArray[np.uint8], target: npt.NDArray[np.uint8]) -> None:
    """BGR pixels into a slot's four-byte pixels (as fast as copying them: about 1.3 ms for 4K)."""
    import cv2  # the engine's preview processes have it loaded; the window never writes slots

    written = cv2.cvtColor(np.ascontiguousarray(pixels), cv2.COLOR_BGR2BGRA, dst=target)
    if written.ctypes.data != target.ctypes.data:  # OpenCV made an array of its own
        np.copyto(target, written)


class DisplayReader:
    def __init__(self, lease: DisplayLease, *, folder: Path = _FOLDER) -> None:
        self.lease = DisplayLease.model_validate(lease.model_dump())
        descriptor = os.open(folder / lease.name, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_size != lease.stride * lease.slots
            ):
                raise ValueError("invalid local display file")
            self._mapping = mmap.mmap(descriptor, info.st_size, access=mmap.ACCESS_READ)
        finally:
            os.close(descriptor)

    def pixels(self, lease: DisplayLease) -> npt.NDArray[np.uint8]:
        if (
            lease.name != self.lease.name
            or lease.stride != self.lease.stride
            or lease.slots != self.lease.slots
        ):
            raise ValueError("display file changed")
        lease = DisplayLease.model_validate(lease.model_dump())
        shape = (lease.height, lease.width, lease.channels)
        return np.ndarray(shape, np.uint8, buffer=self._mapping, offset=lease.slot * lease.stride)

    def close(self) -> None:
        self._mapping.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self, kind: type[BaseException] | None, error: BaseException | None, trace: TracebackType | None
    ) -> None:
        self.close()
